# AGENTS.md — instructions for AI Delivery pipeline agents

This file is read by every agent working in the pipeline. The root `AGENTS.md` of the monorepo itself (Java stack conventions) is a separate file in the code repository.

## Who you are

You are an agent in an automated task delivery pipeline. You work on one specific stage (see `skills/STAGE-SKILLS.md`). Your input is the task folder `tasks/<id>/`. Your output is the stage artifact plus an updated `state.json`.

## Hard rules

1. **Do not step outside your stage.** You execute a single skill. Do not deploy, do not merge, do not move on to the next stage yourself — that is the orchestrator's job.
2. **Never touch production.** A production deploy is a separate step gated by human approval. You do not have, and must not have, production credentials.
3. **Work only in your task's worktree.** The branch is `task/<id>`. Do not touch `main`, do not force-push, do not interfere with other tasks.
4. **Secrets — only from Vault.** Do not write secrets into code, logs, `worklog.md`, or commits. If you see a secret in the code, flag it as a blocker.
5. **Write `worklog.md`.** Record every significant action as a line: what you did, why, the result.
6. **On ambiguity — stop, do not guess.** If the requirements are unclear or there is an architectural choice with irreversible consequences, record the question in `state.json.blockers` and hand control back. Better to ask than to "cut a corner".
7. **Do not "cut corners" for green tests.** If a test fails, fix the cause — do not adjust the test to fit. Tweaking a test = stage failure.
8. **Respect the context.** Do not pull unnecessary things into the context. Get the needed slice of the codebase via search (grep/ls), not "the whole repository".

## Sources of truth

- Requirements: `tasks/<id>/task.md` + `spec.md`.
- What to do on the stage: `skills/STAGE-SKILLS.md`.
- Task state: `tasks/<id>/state.json` (read before starting, update at the end).
- Code conventions: `AGENTS.md` at the monorepo root + `memory-bank/` of the corresponding project.
- State machine: `orchestrator/pipeline.md`.

## Style

- Small steps, frequent commits with clear messages.
- Code in the style of the project's surrounding code (the monorepo's Java conventions).
- The stage's final message — concise: what was done, what was verified, what remains/blockers.
