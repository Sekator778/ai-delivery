---
name: review
description: The REVIEW stage of the AI Delivery pipeline — cross-model review of the implemented code. Run on a model different from the implementer model.
---

# REVIEW stage

Cross-model review: the implementer's code is checked by ANOTHER model. Single-model review gives
a false sense of safety — a model structurally cannot see the class of its own bugs
(research_report, layer 3).

**Phase model:** reviewer ≠ implementer (Opus for the critical work, a second model for the routine).
**Input:** the change diff, `spec.md`, the contract tests.
**Output:** a verdict (`pass` / `changes-requested`) + a list of findings.

## What to check

- **Conformance to the spec** — whether all acceptance criteria of `spec.md` are implemented.
- **Test cheating** — whether there is hardcoding of expected values, stubs, or disabled checks.
- **Conventions** — layers, injection, naming, SLF4J (see `AGENTS.md`).
- **Security** — secrets in the code, input validation, injections.
- **Highload risks** — N+1 queries, locks, unreleased resources.

## Rules

- The reviewer does not edit the code — only records the findings and the verdict.
- Each finding is specific: the file, the line, what is wrong, why.
- Architectural problems matter more than stylistic nitpicks.
