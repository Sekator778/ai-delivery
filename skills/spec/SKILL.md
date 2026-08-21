---
name: spec
description: The SPEC stage of the AI Delivery pipeline — turns a raw business request into a clear, verifiable specification (goal, scope, acceptance criteria). Run on the planning model (Opus).
---

# SPEC stage

The first substantive step of the pipeline: from a raw request — a verifiable specification.
The spec is the single source of truth for the task (research_report, layer 8: SDD).

**Phase model:** planning (Opus).
**Input:** `task.md` — the raw business request.
**Output:** `spec.md`.

## Procedure

1. Read `task.md`, the root `AGENTS.md`, and the `memory-bank/` of the target repository.
2. Compose `spec.md`:
   - **Goal** — what and why, 1–3 sentences.
   - **In scope** — what is included.
   - **Out of scope** — what we do NOT do (explicitly, so the agent does not sprawl).
   - **Acceptance criteria** — verifiable items; each must be expressible as a test.
3. If the request is ambiguous, record the assumptions as an explicit list — do not guess silently.

## Rules

- The specification describes BEHAVIOR, not the implementation. No choice of classes and libraries.
- Every acceptance criterion is verifiable. "Works well" is not a criterion.
- Stay within the scope boundaries from `memory-bank/goal.md`.
