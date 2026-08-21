# AI Delivery pipeline stage skills

The skills registry. One skill = an instruction to the agent for one pipeline stage.
The principle (research_report, layer 5): **"a thin skill + a thick convention"** — the skill
is short, the general rules are moved into `sample-monorepo/AGENTS.md`.

The format — the Agent Skills standard: `<name>/SKILL.md` with YAML frontmatter (`name`, `description`).

## Registry

| Skill | Stage | Phase model | Purpose |
|---|---|---|---|
| `spec` | SPEC | planning (Opus) | raw request → specification |
| `plan` | PLAN | planning (Opus), plan mode | specification → implementation plan |
| `test-design` | TEST-DESIGN | reviewer (≠ implementer) | contract tests before the code |
| `implement` | IMPLEMENT | coder (Chinese model) | production code against the tests |
| `review` | REVIEW | reviewer (≠ implementer) | cross-model code review |

## Model routing

Which model to assign to a stage is decided by the conductor (`orchestrator/`), not the skill.
The skill only states the TARGET phase model. The routing table — research_report, Stage 4.

## Audit

Keep the skills thin. The chat's anti-pattern is sprawl (skills of 500–900 lines, "5000
skills"). When you add a skill, write a line into the registry above; periodically review and remove
unused ones (GC).

## Non-agentic stages

`triage`, the deterministic part of `gate` (mvn verify, the test-integrity check),
`prod` (the deploy) — these are conductor logic, not agent skills. See `orchestrator/pipeline.md`.
