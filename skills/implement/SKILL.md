---
name: implement
description: The IMPLEMENT stage of the AI Delivery pipeline — writes production code that satisfies the contract tests. Run on the cheap coder model (a Chinese coding model).
---

# IMPLEMENT stage

Implementation of production code against the already written contract tests.

**Phase model:** coder (a cheap Chinese coding model — GLM/Kimi).
**Input:** `spec.md`, `plan.md`, the contract tests in `src/test/**`.
**Output:** production code in `src/main/**`.

## Procedure

1. Read `spec.md`, `plan.md`, `AGENTS.md`, and the contract tests.
2. Implement the production code per the plan.
3. Run `mvn -q -B test`.
4. If it fails, find the cause and fix the PRODUCTION code. Repeat until green.

## Rules

- **NEVER change `src/test/**`.** The tests are the contract; an edit is caught by the gate via sha256.
- Do not cheat the code: no hardcoding of expected values and no stubs just for green.
- Follow the `AGENTS.md` conventions (layers, constructor injection, record DTOs, SLF4J).
- Small steps; do not go outside the scope of `spec.md`.
