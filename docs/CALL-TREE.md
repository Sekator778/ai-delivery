# Pipeline call tree

Who spawns whom, from the operator's shell down to the persona dispatch —
every node with its owning module and the reason it exists.

## Why this file exists

ARCHITECTURE.md describes intent and drifts: by 2026-08-15 it promised a
DeepSeek default that had been gone since June, listed six deleted personas,
and described the pre-move `cwd` behavior in five places. Prose that nothing
checks decays in days — the same day the stage-`cwd` move landed, the docs
describing it were already wrong.

This document is different by contract:

- The **prose** (the tree below) explains *why* each node exists — that part
  only a human can write.
- The **fact block** at the bottom is *generated* by
  [`ops/check-arch-map.py`](../ops/check-arch-map.py) from the code itself:
  spawn sites, dispatcher imports, stage→persona dispatch, hooks, personas
  on disk.
- `ops/check-arch-map.py --check` diffs the block against the code and is run
  by the test suite (`tests/test_arch_map.py`), so the commit that changes the
  topology fails until it also runs `--update` — and whoever runs `--update`
  is standing in this file, one screen away from the prose the change may
  have falsified.

If `--check` fails: run `ops/check-arch-map.py --update`, read the diff it
applied, and fix any sentence above the block that the diff contradicts.

## The stack (operator → daemons)

```
operator shell — aidup / aiddown / aidstatus (ops/claude-aliases.sh)
└─ ops/atlas/aidstack.sh                 macOS replacement for ops/systemd/ units
   ├─ docker compose (services/stacks/mem0)   Qdrant ONLY — the compose file
   │                                          declares one service
   ├─ (probe, never starts) TEI on MEMORY_TEI_URL   the other half of
   │                                          memory_inject; external
   ├─ dispatcher daemon = bot/venv python dispatcher/task_dispatcher.py
   ├─ watcher daemon    = bot/venv python dispatcher/watcher.py
   ├─ bot daemon        = bot/venv python bot/bot.py   (only if bot/.env has a real token)
   └─ (down) python dispatcher/proc_reaper.py          orphaned-claude sweep
```

- **aidstack.sh** exists because this machine has no systemd; it also sources
  `bot/.env` into both daemons (per-target policy such as
  `MERGEABLE_REPO_PATHS` lives there — without it every target runs in PoC
  seatbelt mode).
- **TEI is probed, not managed** (2026-08-20). `dispatcher/memory_inject.py`
  needs Qdrant *and* a text-embeddings server; only the first is in the compose
  file. The second is a launchd agent owned by another project, with
  `RunAtLoad=false`, so it does not survive a reboot — and because memory
  degrades to a no-op by contract, a dead TEI is otherwise silent. `up` and
  `status` probe `MEMORY_TEI_URL/info` (TEI serves no `/health`) and warn; the
  stack still comes up. That probe is why `dispatcher/memory_inject.py` now
  appears in the `[entrypoints:...]` fact block below — aidstack.sh references
  the module, it does not run it.
- **The orphan sweep on `down`** exists because a `claude` child whose runner
  died re-parents to init and keeps burning the subscription — 3h11m unnoticed
  on 2026-08-14 (#18). The matcher (ppid==1, no tty, both pipeline flags) can
  never select an interactive session.
- **`down` and `restart` refuse while a task runner is live** (T24). That sweep
  is what makes them destructive, so both ask `dispatcher/runner_liveness.py`
  first and stop rather than kill a stage mid-flight; `--wait` waits, `--force`
  overrides. `up` does *not* have this guard and does not need it — it never
  touches a live daemon, which is also why it does not deploy to a running
  stand and now says so.

## The three daemons

```
task_dispatcher.py     poll tasks/inbox/ → parse spec.json → move to active/
│                      THE only mutator of the tasks/ queue (bot + Windmill only write inbox specs)
└─ Popen [python stage_runner_agent.py <task_dir>]    one detached runner per task,
                                                      concurrency-capped, pid → .runner.pid

watcher.py             crash recovery + reconciliation, 15s sweep
├─ Popen [python stage_runner_agent.py <task_dir>]    respawn after a runner crash
│                                                     or a limit-park resume
├─ clarify.py dead man                                clarify pause unanswered for
│                                                     CLARIFY_DEADMAN_HOURS → writes the
│                                                     BA's own defaults into
│                                                     clarifications.md, requeues ONCE
│                                                     (off unless the env is set)
├─ run [gh pr view …]                                 PR reconciliation: None on ANY
│                                                     failure = "unknown, retry" — never CLOSED
└─ run [ps …] + proc_reaper                           liveness / orphan classification

bot/bot.py             Telegram surface (product language: Russian)
├─ writes tasks/inbox/<id>/spec.json                  its ONLY interface to the pipeline
├─ run [npx ccusage …]                                usage report command
├─ run [codegraph index --force]                      refresh-code command
└─ bin/botctl-* helpers                               shell surface shared with the watchdog
```

The dispatcher and the watcher both spawn the same runner script on purpose:
recovery must produce a process indistinguishable from the original, so the
runner itself is the single place that knows how to resume a task
(`state.json` + per-stage session ids + the branch/PR lock — losing that lock
once killed a fully green task at rc=5 after $14.56, commit `8f7619e`).

## Inside one stage runner

```
stage_runner_agent.py <task_dir>          runs the stage sequence for ONE task
├─ triage verdict (triage.py, triage_wiring.py)
│  └─ run ["claude", --dangerously-skip-permissions, -p <verdict prompt>]
│         ONE cheap Sonnet call; on any failure triage degrades to deterministic-only
├─ worktree isolation (git_pr.py, target_policy.py)
│  └─ run ["git", worktree add /tmp/ai-delivery-wt/<task>-XXXX -b <branch>]
│         developer+ stages work in a throwaway checkout so a self-targeted run
│         can never yank the live deployment's files from under itself
├─ per stage (ba → … → reviewer):
│  ├─ backend_routing.py    which backend/model, and the child env for it
│  │  ├─ child_env.py       minimal env — stages must not inherit the operator's shell
│  │  └─ pipeline_config.py CLAUDE_CONFIG_DIR owned by the pipeline, not the operator
│  │     └─ run ["security", find-generic-password]   macOS: seed credentials into the
│  │            isolated dir — without it every stage dies "Not logged in"
│  ├─ agent_roster.py       cwd = target repo (or its worktree) + --agents payload
│  │                        from .claude/agents/*.md + --add-dir back to this repo
│  ├─ memory_inject.py      fills the prompt's <injected-memory> slot for
│  │                        ba/architect/developer (TEI :8087 embed + Qdrant :6333
│  │                        search, stdlib HTTP — or memory_flat.py's JSONL store
│  │                        with MEMORY_FLAT_ENABLED=1, T13); typed task_lesson
│  │                        write-back at pipeline completion; degrades to
│  │                        "(none)" — a stage never blocks on memory infra
│  │                        (replaces the dead UserPromptSubmit hook path)
│  ├─ proc_reaper.spawn ["claude", --dangerously-skip-permissions,
│  │                     --session-id <uuid>, --agents <json>, --add-dir …,
│  │                     --output-format stream-json, -p STAGE_PROMPTS[stage]]
│  │  │   own process group (#18); live stdout/stderr pump feeds limit_stall.py,
│  │  │   which parks the task instead of burning a timeout on a limit storm
│  │  └─ claude CLI, cwd = TARGET project → loads the TARGET's CLAUDE.md/AGENTS.md
│  │     └─ Agent tool → persona named by `subagent_type = "…"` INSIDE the prompt text
│  │        (reviewer runs three lenses: blind-hunter / edge-case-hunter / verification-gap)
│  ├─ gates: invest_validator (BA), architecture_lint (Architect),
│  │         clarify (ambiguity pause), budget_gate (cost cap → awaiting-input)
│  ├─ cost: apply_backend_pricing (backend_routing.py) recomputes the stage
│  │        cost from tokens × the provider price table for non-anthropic
│  │        backends (the CLI prices every endpoint at Anthropic rates, ~22×
│  │        for DeepSeek), then the figure lands in the artifact AND a
│  │        cost_ledger.py row — one write point, so they can never diverge
│  └─ control_loop.py + post_pipeline.py + auto_loop.py
│         verdict parsing, iteration caps, what state.stage becomes next
├─ run ["gh", pr create / comment]        (git_pr.py) draft PR + findings comments
└─ telegram_io.py → run [bin/botctl-send-text <text>] → Telegram status updates
```

Two facts here are non-obvious and load-bearing:

- **The stage executes inside the target project, not this framework**
  (2026-08-15). Before that, every stage of every task booted with
  ai-delivery's own CLAUDE.md — two-remote push policy and all — while
  developing a completely different repository. `agent_roster.py` is what
  makes the move possible: personas travel via `--agents` instead of being
  read from the cwd's `.claude/agents/`.
- **`STAGE_AGENT_MAP` does not select the persona.** Its only consumer is the
  `choices` list of the `--stage` CLI flag. The actual dispatch is the
  `subagent_type = "…"` line hardwired in each stage's prompt text. The fact
  block records both maps side by side (`[stage-personas]` vs
  `[prompt-dispatch]`) so their divergence — today: the reviewer stage — is
  permanently visible instead of rediscovered.

## Dispatcher module roles

| Module | Why it exists |
|---|---|
| `task_dispatcher.py` | file-queue daemon; polls inbox, spawns runners, owns the queue |
| `watcher.py` | crash recovery + PR reconciliation; respawns runners |
| `runner_liveness.py` | "is a runner alive for this task" — one definition, shared by `watcher.py` and `aidstack.sh`; matches the process cmdline, not just `kill -0`, because pidfiles outlive their processes and pids get reused |
| `stage_runner_agent.py` | runs one task's stage sequence via `claude -p` + Agent tool |
| `stage_prompts.py` | stage data: prompts, artifact names, done-markers (god-module split 2026-06-04) |
| `agent_roster.py` | persona injection + working-directory resolution for stages |
| `backend_routing.py` | backend selection (anthropic/deepseek/glm) + subprocess env |
| `child_env.py` | minimal env for claude children — no operator-shell inheritance (#13) |
| `pipeline_config.py` | pipeline-owned CLAUDE_CONFIG_DIR + macOS keychain credential seeding |
| `triage.py` / `triage_wiring.py` | adaptive complexity sizing; drops redundant *upstream* stages only — review/test/security never drop |
| `clarify.py` | interactive clarification pause on ambiguous specs; the dead-man half the watcher uses to resume one unanswered pause on the BA's defaults (T10) |
| `invest_validator.py` | INVEST gate on BA artifacts |
| `architecture_lint.py` | deterministic structural linter for the Architect artifact (adapted from BMAD) |
| `control_loop.py` | verdict parsing + iteration primitives |
| `post_pipeline.py` | what `state.stage` becomes after a reviewer pass |
| `auto_loop.py` | iteration caps + idle watchdog (ported from claude-tg-orchestrator) |
| `git_pr.py` | branch/PR-URL parsing, base-branch safety, PR recovery, draft-PR posting |
| `target_policy.py` | target-repo policy, PoC seatbelt, branch-safety gates |
| `project_registry.py` | the ONE parser for `bot/projects.json` |
| `budget_gate.py` | cost-cap park → `awaiting-input` with operator notification |
| `cost_ledger.py` | append-only SQLite ledger of per-stage costs (backend + key profile) |
| `provider_profiles.py` | named key profiles per provider — which key of a backend pays for a stage (T15); no registry ⇒ the global env key |
| `limit_stall.py` | limit-outage detection on the live stream; parks instead of timing out (#11) |
| `notify_policy.py` | which events reach Telegram (#19) |
| `proc_reaper.py` | process-group ownership + orphan reaping (#18) |
| `runner_state.py` | worklog/history/state.json mutations |
| `telegram_io.py` | notification side-channels (botctl-send-text, bot HTTP) |

## Hooks (removed 2026-08-17)

`.claude/settings.json` wired four mem0 hooks (`inject_from_mem0` +
three `capture_*`). They had been dead since before the stage-`cwd` move
(system `python3` without `fastembed`, error swallowed with `exit 0`), and
the stage-side job moved into the runner as `dispatcher/memory_inject.py`
(memory-inject, roadmap #0). Owner decision 2026-08-17: remove rather than
repair — interactive sessions are covered by Claude Code auto-memory, the
bot keeps its own `/memo`/`/recall` path, and the legacy Qdrant points the
hooks once wrote remain in the store and are served by the runner's global
recall. `dispatcher/hooks/` is deleted; the fact block below records the
empty wiring, so reviving a hook shows up as drift.

## Recorded facts

Generated — edit via `ops/check-arch-map.py --update`, never by hand.

<!-- arch-facts:begin -->
```
# arch-facts v1 — generated by ops/check-arch-map.py --update; do not edit by hand

[entrypoints:ops/atlas/aidstack.sh]
bot/bot.py
dispatcher/memory_inject.py
dispatcher/proc_reaper.py
dispatcher/runner_liveness.py
dispatcher/task_dispatcher.py
dispatcher/watcher.py

[imports:dispatcher]
backend_routing: child_env, pipeline_config, provider_profiles, target_policy, triage
budget_gate: telegram_io
control_loop: stage_prompts
git_pr: target_policy
limit_stall: runner_state, telegram_io
memory_inject: memory_flat
post_pipeline: auto_loop, runner_state
stage_runner_agent: agent_roster, architecture_lint, backend_routing, budget_gate, clarify, control_loop, cost_ledger, git_pr, invest_validator, limit_stall, memory_inject, notify_policy, post_pipeline, proc_reaper, provider_profiles, runner_state, stage_prompts, target_policy, telegram_io, triage, triage_wiring
target_policy: project_registry
triage_wiring: backend_routing
watcher: budget_gate, clarify, limit_stall, proc_reaper, runner_liveness, runner_state, telegram_io

[spawn-sites]
bot/bot.py :: refresh_code_command :: codegraph
bot/bot.py :: usage_command :: args
dispatcher/git_pr.py :: _branch_base_ok :: git
dispatcher/git_pr.py :: _current_git_branch :: git
dispatcher/git_pr.py :: _post_nonblocking_review_comment :: gh
dispatcher/git_pr.py :: _post_unresolved_findings_comment :: gh
dispatcher/git_pr.py :: _pr_base_ref :: gh
dispatcher/git_pr.py :: _recover_pr_from_repo :: gh
dispatcher/git_pr.py :: _try_open_draft_pr :: gh
dispatcher/git_pr.py :: _try_open_draft_pr :: git
dispatcher/git_pr.py :: _verify_and_repair_pr_base :: gh
dispatcher/pipeline_config.py :: _seed_credentials_from_keychain :: security
dispatcher/proc_reaper.py :: _read_ps :: PS_ARGV
dispatcher/proc_reaper.py :: spawn :: argv
dispatcher/runner_liveness.py :: pid_is_alive :: ps
dispatcher/stage_runner_agent.py :: _git :: git
dispatcher/stage_runner_agent.py :: _run_claude_stage :: argv
dispatcher/stage_runner_agent.py :: _run_claude_stage_buffered :: argv
dispatcher/target_policy.py :: _origin_default_branch :: git
dispatcher/task_dispatcher.py :: _spawn_stage_runner :: sys.executable+_STAGE_RUNNER_SCRIPT
dispatcher/telegram_io.py :: _send_telegram :: BOTCTL_SEND_TEXT
dispatcher/triage_wiring.py :: _triage_run_claude :: claude
dispatcher/watcher.py :: _gh_pr_view :: gh
dispatcher/watcher.py :: _spawn_runner :: sys.executable+STAGE_RUNNER_SCRIPT

[stage-personas:STAGE_AGENT_MAP]
analyze: architect
architect: architect
ba: business-analyst
developer: backend-developer
developer-hotfix: backend-developer
discovery: context-manager
edge-cases: code-reviewer
pattern-detector: pattern-detector
reviewer: code-reviewer
security: security-auditor
tasks: architect
tester: test-automator

[prompt-dispatch:subagent_type-in-STAGE_PROMPTS]
analyze: architect
architect: architect
ba: business-analyst
developer: backend-developer
developer-hotfix: backend-developer
discovery: context-manager
edge-cases: code-reviewer
pattern-detector: pattern-detector
reviewer: blind-hunter, edge-case-hunter, verification-gap
security: security-auditor
tasks: architect
tester: test-automator

[hooks:.claude/settings.json]

[personas:.claude/agents]
architect, backend-developer, blind-hunter, business-analyst, code-reviewer, context-manager, edge-case-hunter, pattern-detector, security-auditor, team-debugger, team-implementer, team-lead, team-reviewer, test-automator, verification-gap
```
<!-- arch-facts:end -->
