# Team workflow — plan-mode workflow

How the 6-person team drives a task with an agent. Source — `research/research_report.md`,
section 4.1 point 3 and layer 5 (plan mode, the spec/code/tests "sandwich").

This is an **interactive** process — team augmentation (research_report 4.1). The autonomous
24/7 pipeline is a separate layer (`orchestrator/`, roadmap 4.2–4.3); it
reuses the same stages and the same skills from `skills/`.

## The "sandwich" principle

```
   spec    ← human (analyst)         top: defines the task
   plan    ← Opus, plan mode         decomposition
   code    ← Chinese model           implementation
   tests   ← human reviews           bottom: control
```

The human owns the top (what to do) and the bottom (verification). The agent does the middle.

## Task flow

| Step | Who | What | Skill |
|---|---|---|---|
| 1. Spec | analyst | a short spec — even 100 words radically improve the plan | `spec` |
| 2. Plan | Opus, plan mode | decompose the spec into a plan (read-only analysis) | `plan` |
| 3. Plan review | developer | reviews the plan BEFORE code generation | — |
| 4. Tests | reviewer model | contract tests against the spec | `test-design` |
| 5. Code | Chinese model | production code against the tests | `implement` |
| 6. Gate | automation + reviewer | `mvn verify` + cross-model review | `review` |
| 7. Approval | developer / QA | merge into `main` | — |

## Why plan mode on Opus

Plan mode is Claude Code's read-only mode: the agent analyzes the code, asks questions, produces
a plan, but does not change files. It is a checkpoint BEFORE the agent touches the monorepo.
It runs on Opus: an expensive model on a small token volume pays off on planning,
while bulk code generation is handed to a cheap Chinese model (research_report, Stage 4).

## Team roles

- **2 analysts** — owners of the specifications (step 1).
- **2 developers** — review of plans and architecture (steps 3, 7).
- **2 QA** — owners of the quality gate, E2E as executable use cases (steps 4, 6, 7).

## Rule

No task lands in `main` without triple control: a deterministic gate
(`mvn verify`) + cross-model review + human approval.
For details — `sample-monorepo/AGENTS.md`, section 7.
