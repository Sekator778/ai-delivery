"""C.3 — INVEST validator for BA artifacts.

Runs over `01-ba.md` (or `01-ba-agent.md`) after BA completes and surfaces
violations of INVEST principles that can be detected statically:

- **vague verbs**: support/handle/consider/manage/process/… without a concrete
  action verb. INVEST-Testable says: "the team can write a test that fails
  before the change and passes after"; vague verbs make this impossible.
- **acceptance-criteria coverage**: every FR-* should be paired with at least
  one AC; missing ACs make stories non-Testable.
- **story size**: more than ~15 FRs in one spec is INVEST-Small violation —
  the spec should be split.

Output is a markdown report (`01-ba-invest.md`) sitting next to the BA
artifact, keyed behind `INVEST_VALIDATION_ENABLED=1` in the orchestrator.
When enabled, the gate BLOCKS the pipeline on violations by default; set
`INVEST_BLOCKING=0` for the legacy warn-only behaviour (report-only — the
Reviewer reads it and the operator decides).

Future iterations may auto-loop BA with the violations as feedback, or
add a pre-commit hook over `memory-bank/`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Words that are red flags for INVEST-Testable. Standalone single-token verbs
# only; many of these are fine when paired with a concrete object (e.g.
# "process payment requests" is fine, "process the data" is not). We surface
# *candidates* — the human reviewer judges. Lowercase compare, word-boundary.
VAGUE_VERBS = {
    "support", "handle", "consider", "manage", "process",
    "deal", "work", "integrate", "facilitate", "leverage",
    "enable", "interact", "interface",
}

# Vague qualifier phrases (multi-word). Order matters: more specific first so
# substrings don't double-match.
VAGUE_PHRASES = [
    "as needed", "if necessary", "and so on", "etc.",
    "user-friendly", "appropriate", "robust enough",
    "various", "where applicable",
]

# Modal verbs that weaken acceptance criteria (RFC 2119: should/may vs must).
WEAK_MODALS = {"might", "may", "could", "should consider"}

# Heuristic cap: above this, the spec is INVEST-Small violation.
MAX_FR_COUNT = 15

# Section header patterns BA uses (we cite vendored Spec-Kit; tolerate both
# our prompt's "## Acceptance Criteria" and slight variants).
_AC_HEADER_RE = re.compile(
    r"^\s{0,3}#{2,4}\s+acceptance\s+criteria\b", re.IGNORECASE,
)
_FR_LINE_RE = re.compile(r"^\s*-?\s*\*?\*?FR-\d+\b", re.IGNORECASE)
_AC_LINE_RE = re.compile(r"^\s*-?\s*\*?\*?AC-\d+\b", re.IGNORECASE)
# Canonical Spec-Kit marker is `[NEEDS CLARIFICATION: <question>]` — the colon
# is mandatory. Requiring it avoids the false positive where BA self-validation
# prose ("No [NEEDS CLARIFICATION] markers remain") trips the detector.
_NEEDS_CLARIFICATION_RE = re.compile(r"\[NEEDS\s+CLARIFICATION\s*:", re.IGNORECASE)


@dataclass
class Violation:
    line: int
    kind: str          # "vague_verb" | "vague_phrase" | "weak_modal" | "no_ac" | "story_too_big" | "clarification_left"
    snippet: str       # short context for the report
    suggestion: str = ""


@dataclass
class Report:
    artifact: str
    violations: list[Violation] = field(default_factory=list)
    fr_count: int = 0
    ac_count: int = 0
    has_ac_section: bool = False
    clarification_markers: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations


def _word_boundary_search(verbs: set[str], line: str) -> list[str]:
    """Return verbs from `verbs` found on a word boundary in lowercased `line`."""
    lowered = line.lower()
    return [v for v in verbs if re.search(rf"\b{re.escape(v)}\b", lowered)]


def validate(text: str, artifact_path: str = "<inline>") -> Report:
    r = Report(artifact=artifact_path)

    lines = text.splitlines()

    in_ac_section = False
    for idx, raw_line in enumerate(lines, start=1):
        if _AC_HEADER_RE.match(raw_line):
            r.has_ac_section = True
            in_ac_section = True
            continue
        # Any other top-level header closes the AC section.
        if raw_line.lstrip().startswith("## ") and in_ac_section:
            in_ac_section = False

        if _FR_LINE_RE.match(raw_line):
            r.fr_count += 1
        if _AC_LINE_RE.match(raw_line) or (in_ac_section and raw_line.strip().startswith("- ")):
            r.ac_count += 1
        if _NEEDS_CLARIFICATION_RE.search(raw_line):
            r.clarification_markers += 1
            r.violations.append(Violation(
                line=idx, kind="clarification_left",
                snippet=raw_line.strip()[:120],
                suggestion="BA should resolve via defaults or operator clarification (C.2) before declaring the spec done.",
            ))

        verbs = _word_boundary_search(VAGUE_VERBS, raw_line)
        for v in verbs:
            r.violations.append(Violation(
                line=idx, kind="vague_verb", snippet=raw_line.strip()[:120],
                suggestion=f"Replace '{v}' with a concrete verb describing the observable action (e.g., 'persist', 'return', 'reject', 'route', 'rate-limit').",
            ))
        modals = _word_boundary_search(WEAK_MODALS, raw_line)
        for m in modals:
            r.violations.append(Violation(
                line=idx, kind="weak_modal", snippet=raw_line.strip()[:120],
                suggestion=f"Replace '{m}' with 'must' / 'must not' / 'does' / 'does not' — INVEST-Testable requires unambiguous behaviour.",
            ))
        lowered = raw_line.lower()
        for phrase in VAGUE_PHRASES:
            if phrase in lowered:
                r.violations.append(Violation(
                    line=idx, kind="vague_phrase", snippet=raw_line.strip()[:120],
                    suggestion=f"Replace '{phrase}' with the concrete case the spec must cover.",
                ))

    if r.fr_count > MAX_FR_COUNT:
        r.violations.append(Violation(
            line=0, kind="story_too_big",
            snippet=f"FR count = {r.fr_count} (limit {MAX_FR_COUNT})",
            suggestion="Split the spec into multiple INVEST-Small stories. Aim for ≤15 FRs per story.",
        ))

    # No-AC violation only when we found at least one FR but zero ACs.
    if r.fr_count > 0 and r.ac_count == 0:
        r.violations.append(Violation(
            line=0, kind="no_ac",
            snippet=f"FR count = {r.fr_count}, AC count = 0",
            suggestion="Every FR must have at least one matching acceptance criterion (Given/When/Then or AC-NNN bullet under '## Acceptance Criteria').",
        ))

    return r


def format_report(r: Report, blocking: bool | None = None) -> str:
    """Markdown report written next to the BA artifact.

    ``blocking`` controls the severity footer so the report matches the gate's
    actual behaviour. When None it is derived from ``INVEST_BLOCKING`` — the
    same default the gate uses (blocking unless explicitly ``0``)."""
    if blocking is None:
        blocking = os.environ.get("INVEST_BLOCKING", "1").strip() != "0"
    lines: list[str] = [
        "# INVEST validation report",
        "",
        f"**Artifact**: `{r.artifact}`",
        f"**FR count**: {r.fr_count}    **AC count**: {r.ac_count}    "
        f"**AC section present**: {'yes' if r.has_ac_section else 'no'}    "
        f"**[NEEDS CLARIFICATION] left**: {r.clarification_markers}",
        "",
    ]
    if r.ok:
        lines.append("✅ No violations detected.")
        return "\n".join(lines) + "\n"

    lines.append(f"⚠️  {len(r.violations)} violation(s):")
    lines.append("")
    by_kind: dict[str, list[Violation]] = {}
    for v in r.violations:
        by_kind.setdefault(v.kind, []).append(v)
    kind_titles = {
        "vague_verb":  "Vague verbs (INVEST-Testable)",
        "vague_phrase": "Vague phrases (INVEST-Testable)",
        "weak_modal":   "Weak modals (INVEST-Testable)",
        "no_ac":        "Missing acceptance criteria (INVEST-Testable)",
        "story_too_big": "Story too big (INVEST-Small)",
        "clarification_left": "Unresolved [NEEDS CLARIFICATION] markers",
    }
    for kind, group in by_kind.items():
        lines.append(f"## {kind_titles.get(kind, kind)} — {len(group)}")
        lines.append("")
        for v in group:
            loc = f"line {v.line}" if v.line else "(spec-level)"
            lines.append(f"- **{loc}** — `{v.snippet}`")
            if v.suggestion:
                lines.append(f"  - *Suggestion*: {v.suggestion}")
        lines.append("")
    if blocking:
        lines.append(
            "Severity: **BLOCKING** — the spec failed INVEST validation and the "
            "pipeline was halted at the BA stage. Resolve the violations above "
            "and resubmit (set `INVEST_BLOCKING=0` for warn-only)."
        )
    else:
        lines.append(
            "Severity: advisory (**warn-only**) — the pipeline continues. The "
            "Reviewer reads this report and the operator decides whether to "
            "bounce the spec back to BA."
        )
    return "\n".join(lines) + "\n"


def validate_artifact(artifact_path: Path) -> Report:
    """Convenience: read file and validate. Returns a Report; empty report
    (ok=True, zero counts) if the artifact is missing."""
    if not artifact_path.exists():
        return Report(artifact=str(artifact_path))
    return validate(artifact_path.read_text(encoding="utf-8"), str(artifact_path))


def _smoke() -> None:
    good = """
# BRD

## Functional Requirements

- FR-001 — The system must persist user sessions to PostgreSQL within 50ms p95.
- FR-002 — The system must reject expired sessions and return HTTP 401.

## Acceptance Criteria

- AC-001 — Given a valid session, when fetched within TTL, then persistence latency p95 < 50ms.
- AC-002 — Given an expired session, when used, then API returns 401 with code SESSION_EXPIRED.
"""
    r = validate(good)
    assert r.ok, [v.kind for v in r.violations]
    assert r.fr_count == 2
    assert r.has_ac_section

    bad = """
# BRD

## Functional Requirements

- FR-001 — The system should support various payment methods as needed.
- FR-002 — The system might handle errors appropriate to the situation.
"""
    r = validate(bad)
    assert not r.ok
    kinds = {v.kind for v in r.violations}
    assert "vague_verb" in kinds, kinds        # support, handle
    assert "weak_modal" in kinds, kinds         # might
    assert "vague_phrase" in kinds, kinds       # as needed, various, appropriate
    assert "no_ac" in kinds, kinds              # FR present, AC absent

    too_big = "## Functional Requirements\n" + "\n".join(
        f"- FR-{i:03d} — does X{i} concretely." for i in range(1, 17)
    ) + "\n## Acceptance Criteria\n- AC-001 — t."
    r = validate(too_big)
    assert any(v.kind == "story_too_big" for v in r.violations)
    assert r.fr_count == 16

    with_marker = "## FR\n- FR-001 [NEEDS CLARIFICATION: which auth?]\n## AC\n- AC-001 — t."
    r = validate(with_marker)
    assert r.clarification_markers == 1
    assert any(v.kind == "clarification_left" for v in r.violations)

    # Negative: BA self-validation prose must NOT trip the detector (Bug-2).
    no_marker = (
        "## FR\n- FR-001 — does X concretely.\n"
        "All ambiguities resolved; no [NEEDS CLARIFICATION] markers remain.\n"
        "## AC\n- AC-001 — t."
    )
    r = validate(no_marker)
    assert r.clarification_markers == 0, r.clarification_markers
    assert not any(v.kind == "clarification_left" for v in r.violations)

    print("invest_validator._smoke: ok")


if __name__ == "__main__":
    _smoke()
