# tasks/ — the task queue

Each task is a **folder** `tasks/<COLUMN>/<TASK-ID>/`. The folder is physically moved between columns as it passes through the pipeline. This is the system's "board".

## Columns (coarse states)

| Folder | What it means | Who moves it on |
|---|---|---|
| `inbox/` | the task is created by the bot, waiting for the orchestrator | orchestrator |
| `active/` | the orchestrator drives it through the stages (triage…staging) | orchestrator |
| `awaiting-input/` | ⏸ the agent needs clarification from a human | the submitter |
| `awaiting-approval/` | ⏸ a prod-deploy approval is needed (or a PR review) | the on-call person |
| `done/` | the task is in production | — |
| `failed/` | a human triage is needed | the on-call person |

A quick glance at what needs human attention:
`ls tasks/awaiting-input/  tasks/awaiting-approval/  tasks/failed/`

## Contents of a task folder

Created from `_TEMPLATE/`. Files appear as the stages are passed:

| File | When | Who writes it |
|---|---|---|
| `task.md` | at intake | the intake bot — the original request + a link to Jira |
| `state.json` | at intake, always updated | the orchestrator/agents — stage, history, models, cost |
| `spec.md` | the `spec` stage | Opus |
| `plan.md` | the `plan` stage | Opus |
| `worklog.md` | from the `implement` stage | the agents — a chronology of actions |
| `gate-report.md` | the `gate` stage | the gate — tests, static analysis, review |
| `review.md` | the `gate` stage | Opus — the cross-model review |

## Task ID

`TASK-<number>` or the Jira issue key (`PROJ-123`). The ID = the folder name and the `task/<id>` branch.

## Important

- `state.json` — the exact state; the column folder — the coarse one. On a mismatch, the truth is `state.json`.
- The task's git worktree lives NOT here, but in `paths.worktrees` (`config.yaml`). Here — only the artifacts and metadata.
- The column folders are tracked in git via `.gitkeep`.
