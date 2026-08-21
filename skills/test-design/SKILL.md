---
name: test-design
description: The TEST-DESIGN stage of the AI Delivery pipeline — writes tests as a contract BEFORE implementation. Run on the reviewer model (NOT the implementer model) for epistemic isolation.
---

# TEST-DESIGN stage

The tests are written BEFORE the code and by a SEPARATE model — they are the contract that the implementation must
satisfy. Epistemic isolation: the author of the tests ≠ the author of the code (research_report,
layer 3: the spec/code/tests "sandwich").

**Phase model:** reviewer (a model different from the implementer).
**Input:** `spec.md` + `plan.md` + the repository structure.
**Output:** test files (JUnit 5) in `src/test/java/...`.

## Procedure

1. Read `spec.md`, `plan.md`, `AGENTS.md`.
2. For each acceptance criterion — test(s) that verify the BEHAVIOR.
3. Cover the edge cases and negative scenarios, not just the happy path.
4. If you extend an existing test file — keep the tests that are already there.
5. For integration — Testcontainers (see `AGENTS.md`).

## Rules

- The tests express the SPECIFICATION, not the assumed implementation.
- A test must not pass on empty/stub code — otherwise it is useless.
- Leave no loopholes: the implementer must not be able to satisfy a test by hardcoding.
- Test files only. Do not touch the production code at this stage.
