"""Deterministic structural linter for the Architect artifact (02-architecture.md).

Adapted from BMAD-METHOD's `bmad-architecture/scripts/lint_spine.py` (steal-list
§2.6 / verdict #10, "adapt"): "catches placeholders, duplicate AD IDs, missing
Binds/Prevents/Rule, unpinned Stack versions deterministically, so reviewers
spend judgment on the semantic half." BMAD's linter targets their own
spine-schema (stable ADR ids + Binds/Prevents/Rule triples); we do NOT adopt
that schema (see stage_prompts.py's "architect" ADR section — MADR format,
Status/Context/Considered options/Decision/Consequences/Prevents/Rejected
alternatives, no Binds/Rule/id machinery). This module checks only what our
own template actually prescribes:

- ADR completeness: every detected ADR block carries the required field labels.
- C4 sketch presence: the "## C4 sketches" section exists and contains a
  Mermaid code fence.
- Leftover placeholders (TODO/TBD/TKTK/???) anywhere in the document.

Report-only by default (never blocks the pipeline) — this is a cheap
mechanical pre-check run right after the Architect stage completes, BEFORE
the "analyze" stage's LLM pass spends judgment on the same document. Off
unless ARCHITECTURE_LINT_ENABLED=1 in the orchestrator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# An ADR block starts at a heading line that mentions "ADR" — tolerant of the
# author's exact heading level/style ("### ADR-001: ...", "#### ADR 1 — ...",
# "**ADR-2**: ..."). Ends at the next same-or-higher heading, or EOF.
_ADR_HEADING_RE = re.compile(r"^\s{0,3}(#{2,5}\s*ADR\b|\*\*ADR\b)", re.IGNORECASE)
_ANY_HEADING_RE = re.compile(r"^\s{0,3}#{2,5}\s+\S")

_C4_SECTION_RE = re.compile(r"^\s{0,3}#{2,4}\s+C4 sketches\b", re.IGNORECASE)
_MERMAID_FENCE_RE = re.compile(r"^\s{0,3}```mermaid\b", re.IGNORECASE)

_PLACEHOLDER_RE = re.compile(r"\b(TODO|TBD|TKTK)\b|\?\?\?")

# Required field labels per ADR, in the order the "architect" stage prompt
# lists them. Matched as a label at the start of a line (plain or bolded),
# optionally followed by ':' or '—'.
_REQUIRED_ADR_FIELDS = [
    "Status", "Context", "Considered options", "Decision",
    "Consequences", "Prevents", "Rejected alternatives",
]


def _field_present(block_text: str, label: str) -> bool:
    pattern = rf"^\s{{0,3}}\*{{0,2}}{re.escape(label)}\*{{0,2}}\s*[:—-]"
    return re.search(pattern, block_text, re.IGNORECASE | re.MULTILINE) is not None


@dataclass
class Violation:
    line: int
    kind: str          # "adr_missing_field" | "c4_missing" | "placeholder_left"
    snippet: str
    suggestion: str = ""


@dataclass
class Report:
    artifact: str
    violations: list[Violation] = field(default_factory=list)
    adr_count: int = 0
    has_c4_section: bool = False
    has_c4_diagram: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations


def _split_adr_blocks(lines: list[str]) -> list[tuple[int, str]]:
    """Return (start_line_1indexed, block_text) for each detected ADR block."""
    starts = [i for i, ln in enumerate(lines) if _ADR_HEADING_RE.match(ln)]
    blocks: list[tuple[int, str]] = []
    for pos, start in enumerate(starts):
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if _ANY_HEADING_RE.match(lines[j]) and not _ADR_HEADING_RE.match(lines[j]):
                end = j
                break
            if _ADR_HEADING_RE.match(lines[j]) and j != start:
                end = j
                break
        blocks.append((start + 1, "\n".join(lines[start:end])))
    return blocks


def validate(text: str, artifact_path: str = "<inline>") -> Report:
    r = Report(artifact=artifact_path)
    lines = text.splitlines()

    for start_line, block in _split_adr_blocks(lines):
        r.adr_count += 1
        heading = block.splitlines()[0].strip()[:80]
        for label in _REQUIRED_ADR_FIELDS:
            if not _field_present(block, label):
                r.violations.append(Violation(
                    line=start_line, kind="adr_missing_field",
                    snippet=f"{heading} — missing '{label}'",
                    suggestion=f"Add a '{label}' line to this ADR before handoff.",
                ))

    for idx, raw_line in enumerate(lines, start=1):
        if _C4_SECTION_RE.match(raw_line):
            r.has_c4_section = True
        if _MERMAID_FENCE_RE.match(raw_line):
            r.has_c4_diagram = True
        if _PLACEHOLDER_RE.search(raw_line):
            r.violations.append(Violation(
                line=idx, kind="placeholder_left",
                snippet=raw_line.strip()[:120],
                suggestion="Resolve the placeholder before handoff to Tasks/Analyze.",
            ))

    if not r.has_c4_section:
        r.violations.append(Violation(
            line=0, kind="c4_missing",
            snippet="no '## C4 sketches' section found",
            suggestion="Add Context/Container/Component sketches in Mermaid.",
        ))
    elif not r.has_c4_diagram:
        r.violations.append(Violation(
            line=0, kind="c4_missing",
            snippet="'## C4 sketches' section present but no ```mermaid fence found",
            suggestion="Render the C4 sketches as Mermaid code fences, not prose/ASCII.",
        ))

    return r


def format_report(r: Report) -> str:
    """Markdown report written next to the architecture artifact. Always
    advisory (report-only) — this linter never gates the pipeline; see
    architecture_lint.py module docstring."""
    lines: list[str] = [
        "# Architecture structural lint",
        "",
        f"**Artifact**: `{r.artifact}`",
        f"**ADRs detected**: {r.adr_count}    "
        f"**C4 section**: {'yes' if r.has_c4_section else 'no'}    "
        f"**C4 Mermaid diagram**: {'yes' if r.has_c4_diagram else 'no'}",
        "",
    ]
    if r.ok:
        lines.append("No structural issues detected.")
        return "\n".join(lines) + "\n"

    lines.append(f"{len(r.violations)} finding(s) — advisory, not blocking:")
    lines.append("")
    by_kind: dict[str, list[Violation]] = {}
    for v in r.violations:
        by_kind.setdefault(v.kind, []).append(v)
    kind_titles = {
        "adr_missing_field": "ADR missing a required field",
        "c4_missing": "C4 sketches incomplete",
        "placeholder_left": "Unresolved placeholder",
    }
    for kind, group in by_kind.items():
        lines.append(f"## {kind_titles.get(kind, kind)} — {len(group)}")
        lines.append("")
        for v in group:
            loc = f"line {v.line}" if v.line else "(doc-level)"
            lines.append(f"- **{loc}** — `{v.snippet}`")
            if v.suggestion:
                lines.append(f"  - *Suggestion*: {v.suggestion}")
        lines.append("")
    lines.append(
        "Severity: advisory (report-only) — the pipeline continues. Intended "
        "to save the 'analyze' stage's LLM pass from spending judgment on "
        "purely mechanical gaps; a human or the Reviewer decides what to do "
        "with what's left."
    )
    return "\n".join(lines) + "\n"


def validate_artifact(artifact_path: Path) -> Report:
    """Convenience: read file and validate. Empty report (ok=True, zero
    counts) if the artifact is missing."""
    if not artifact_path.exists():
        return Report(artifact=str(artifact_path))
    return validate(artifact_path.read_text(encoding="utf-8"), str(artifact_path))


def _smoke() -> None:
    good = """
## ADRs (MADR format)

### ADR-001: Use the shared retry client

Status: Accepted
Context: two modules were about to hand-roll retry/backoff independently.
Considered options: (a) shared client (b) per-module retry (c) no retry
Decision: adopt the shared retry client.
Consequences: one more internal dependency edge.
Prevents: a second module reimplementing retry/backoff instead of using the
shared client.
Rejected alternatives: per-module retry — duplicate logic, divergent bugs.

## C4 sketches (Mermaid)

```mermaid
graph TD
  A --> B
```
"""
    r = validate(good)
    assert r.ok, [(v.kind, v.snippet) for v in r.violations]
    assert r.adr_count == 1
    assert r.has_c4_section and r.has_c4_diagram

    bad = """
## ADRs (MADR format)

### ADR-001: Something

Status: Accepted
Decision: do the thing.
Consequences: TBD

## C4 sketches (Mermaid)

Context diagram: see whiteboard photo.
"""
    r = validate(bad)
    assert not r.ok
    kinds = {v.kind for v in r.violations}
    assert "adr_missing_field" in kinds, kinds   # Context/Considered options/Prevents/Rejected missing
    assert "placeholder_left" in kinds, kinds    # TBD
    assert "c4_missing" in kinds, kinds          # section present, no mermaid fence
    missing_fields = {v.snippet.split("'")[1] for v in r.violations if v.kind == "adr_missing_field"}
    assert "Prevents" in missing_fields, missing_fields

    no_c4 = "## ADRs (MADR format)\n\n### ADR-001: X\n" + "\n".join(
        f"{f}: x" for f in _REQUIRED_ADR_FIELDS
    )
    r = validate(no_c4)
    assert any(v.kind == "c4_missing" for v in r.violations)

    print("architecture_lint._smoke: ok")


if __name__ == "__main__":
    _smoke()
