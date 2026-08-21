# dispatcher/

The **task-dispatcher** is the daemon that polls `tasks/inbox/`, drives each task
through the pipeline (BA -> Architect -> Dev -> Test -> Sec -> Reviewer ->
awaiting-approval -> done), and emits status updates back to Telegram.

It is the **only** component that mutates the `tasks/` queue — `bot.py` and
Windmill flows only **write** new `spec.json` files into `tasks/inbox/`.

See [STATE/PHASE-5-DESIGN.md](../STATE/PHASE-5-DESIGN.md) for the full design.

## Layout

```
dispatcher/
├── README.md
├── schema/
│   └── spec.schema.json   # input contract — see below
├── task_dispatcher.py     # (5.B) daemon: poll inbox, parse spec, write state.json
├── auto_loop.py           # (5.C) auto-loop module ported from claude-tg-orchestrator 4aa02b0
└── ...
```

## Input contract — `spec.json`

Every task starts life as a `tasks/inbox/<task-id>/spec.json` file. The schema
is defined formally in [`schema/spec.schema.json`](schema/spec.schema.json) and
summarised here:

| Field | Required | Description |
|---|---|---|
| `trigger` | yes | `telegram`, `windmill` or `manual` |
| `user` | yes | Telegram username or `windmill-cron` |
| `prompt` | yes | Free-text task description from the user |
| `target_repo` | yes | Absolute path or git URL of the target repo |
| `telegram_thread` | yes if `trigger=telegram`; optional for `windmill`/`manual` | `{chat_id, message_id}` for threading status updates |
| `schedule` | no | Cron expression (informational, set by Windmill) |
| `task_id` | no | If absent, dispatcher allocates `TASK-<N>` |
| `created_at` | no | ISO-8601; dispatcher fills if absent |
| `cost_cap_usd` | no | Default `20` |
| `iteration_cap` | no | Default `3` |
| `model_routing` | no | Per-stage backend (`anthropic`/`deepseek`/`glm`), highest precedence. Default is anthropic for every stage; set `DEEPSEEK_STAGES` in the env (e.g. `tester`) to move the mechanical stages onto DeepSeek without editing a spec — see below |

Example (Telegram-triggered):

```json
{
  "trigger": "telegram",
  "user": "your-telegram-handle",
  "prompt": "Add /restart endpoint to userbot-service that calls systemctl restart claude-tg-bot.service.",
  "target_repo": "$HOME/projects/your-target-repo",
  "telegram_thread": {"chat_id": 123456, "message_id": 9876}
}
```

Example (Windmill cron-triggered):

```json
{
  "trigger": "windmill",
  "user": "windmill-cron",
  "prompt": "Bump all npm minor versions, run tests, open PR.",
  "target_repo": "$HOME/projects/your-target-repo",
  "schedule": "0 2 * * *",
  "iteration_cap": 2,
  "cost_cap_usd": 10
}
```

Example (manual / scripted drop — see also [`examples/spec.manual.example.json`](examples/spec.manual.example.json)):

```json
{
  "trigger": "manual",
  "user": "your-username",
  "task_id": "manual-1716743400",
  "prompt": "p99 latency on /search exceeded 500ms for 15 minutes. Investigate, propose fix.",
  "target_repo": "/absolute/path/to/your/target-repo"
}
```

`telegram_thread` is omitted — the pipeline runs unattended and results land in
`tasks/done/<id>/` for the operator to inspect. Supply `telegram_thread` if you
want Telegram status updates for a scripted submission.

## Which model backend runs a stage

Resolved in `backend_routing._resolve_stage_backend`, highest precedence first:

1. **`spec.model_routing[stage]`** — explicit per-task pin, always wins.
2. **Tier L build/verify** — for tier L, the stages in `L_TIER_ANTHROPIC_STAGES`
   (default `developer,developer-hotfix,tester,security`) start on anthropic at
   iteration 0 rather than waiting for the escalation below. DeepSeek timed out a
   real L developer stage mid-build (2026-06-03). Opt out with
   `L_TIER_FORCE_ANTHROPIC=0`.
3. **Iteration escalation** — a stage still failing Reviewer at
   `iteration >= STAGE_ESCALATION_AT_ITERATION` (default 2) is bumped to
   `STAGE_ESCALATION_BACKEND` (default anthropic) for the rest of that task.
4. **`DEEPSEEK_STAGES`** — comma list of stages whose *default* backend becomes
   DeepSeek (e.g. `DEEPSEEK_STAGES=tester`). Empty ⇒ every stage defaults to
   anthropic. Ignored entirely when `DEEPSEEK_API_KEY` is unset, and unknown
   stage names warn instead of failing the boot.
5. **`BACKEND`** — the built-in default: anthropic for every stage.

Orthogonal to all of the above, an anthropic-backed stage picks its *model* by
the two-model policy (`OPUS_STAGES`, default `ba,architect` → Opus; everything
else → Sonnet). DeepSeek stages use `DEEPSEEK_MODEL_PRIMARY` /
`DEEPSEEK_MODEL_HAIKU` (`deepseek-v4-pro` / `deepseek-v4-flash`), reached through
DeepSeek's Anthropic-compatible endpoint or a LiteLLM proxy when
`LITELLM_PROXY_URL` + `LITELLM_MASTER_KEY` are set.

## Where a task executes — ephemeral worktree isolation

`spec.target_repo` names the target **repository**, not the directory the
subagents work in. Before the Developer stage the runner creates a throwaway
worktree of that repo:

```
git -C <target_repo> fetch origin <base>
git -C <target_repo> worktree add /tmp/ai-delivery-wt/<task-id>-XXXX -b <branch> origin/<base>
```

and hands **that** path to the developer / tester / security / reviewer stages
(the earlier, read-only stages still grep the target checkout itself). The
target checkout is never switched, rebased or dirtied — the failure this
prevents is a self-targeted run whose branch switch made the running
deployment's own files vanish mid-run. It also lets several tasks run against
one target repo in parallel.

The worktree is removed (`git worktree remove --force` + `prune`) once the PR is
pushed, or on terminal handoff; the **branch is kept** — it carries the PR, and a
re-queue re-attaches a fresh worktree to it. The record lives in
`state.json.worktree = {path, branch, target_repo, base}`, so a watcher respawn
resumes onto the same branch.

| Env | Default | Meaning |
|---|---|---|
| `WORKTREE_ISOLATION_ENABLED` | `1` | `0` = legacy in-place execution in the target checkout (the developer prompt then asks the subagent to cut the branch itself, as before) |
| `WORKTREE_ROOT` | `/tmp/ai-delivery-wt` | Parent directory for the throwaway checkouts |

Because the branch exists before the subagent starts, the developer prompt says
"you are already on `<branch>` — never switch branches", and the post-stage
safety gate checks the committed branch by identity, not just by prefix. If the
worktree cannot be created, the developer stage FAILS instead of falling back to
the live checkout.

**Base branch** — what the work branch is cut from and what the PR targets — is
resolved once per run, in this order: the target's `base` in `bot/projects.json`
(`{"path": ..., "base": "dev"}`) → `PIPELINE_BASE_BRANCH` → the target repo's own
`origin/HEAD` → `main`. It is persisted as `state.json.base_branch`, so the
prompt and the post-run stale-base check can never drift apart.

Single-stage runs (`--stage X --target-repo Y`) are unchanged: they execute in
the directory given, with no worktree.

## State machine

See [`../tasks/README.md`](../tasks/README.md) for the folder-as-state-machine.
The dispatcher moves `<task-id>/` between columns:

```
inbox/ -> active/ -> awaiting-approval/ -> done/
                  \-> awaiting-input/ (cost cap, repeat-finding, architectural finding)
                  \-> failed/         (terminal error)
```

## Artifacts written into `tasks/active/<task-id>/`

Per-stage outputs (see [`../tasks/README.md`](../tasks/README.md) for the canonical list):

- `task.md` — derived from `spec.json` at intake
- `state.json` — live state (stage, iteration, cost_usd, history)
- `spec.md` — BA output
- `plan.md` — Architect output
- `worklog.md` — chronological log
- `gate-report.md` — Tester + Security output
- `review.md` — Reviewer verdict + findings

For iterations (auto-loop), the dispatcher appends iteration suffixes when
overwriting: `worklog.md`, `gate-report-iter2.md`, `review-iter2.md`, etc.
