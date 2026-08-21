---
name: plan
description: The PLAN stage of the AI Delivery pipeline — turns a specification into an implementation plan (steps, files, risks). Run in plan mode on the planning model (Opus).
---

# PLAN stage

Decomposition of the specification into an executable plan. This is the most valuable use of the expensive model
(research_report 4.1: plan mode on Opus — where Opus pays off).

**Phase model:** planning (Opus), mode — plan mode (read-only analysis).
**Input:** `spec.md` + the codebase.
**Output:** `plan.md`.

## Procedure

1. Read `spec.md`, `AGENTS.md`, the `memory-bank/` of the target repository.
2. Study the existing code that the task touches (read-only, do not change files).
3. Compose `plan.md`:
   - **Steps** — small, in order; each — a separate focused change.
   - **Files** — which are created/changed; production and test ones separately.
   - **Risks** — what can go wrong, especially for highload code.
4. An architectural choice with irreversible consequences — name the alternatives, do not decide silently.

## Rules

- Small steps: the plan breaks the task down, it does not produce one big chunk.
- Do not write code at this stage — only the plan.
- A cheap model implements the plan; write it so that it is executable without guessing.
