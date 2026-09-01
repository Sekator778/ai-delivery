# ai-delivery — Architecture

> **Intent-level view** — why the system is shaped the way it is.
> The *mechanical* truth lives elsewhere and is checked, not promised:
>
> - [docs/CALL-TREE.md](docs/CALL-TREE.md) — who spawns whom, which persona
>   each stage dispatches, module roles. Its fact block is **generated from
>   the code** and gated by the test suite (`ops/check-arch-map.py --check`):
>   change the topology and the suite fails until the doc is regenerated.
> - [docs/PROVENANCE.md](docs/PROVENANCE.md) — where each mechanism came
>   from (BMAD, spec-kit, persona catalogs, our own incidents).
> - `STATE/` (private remote only) — live plans and decisions.
>
> When this file disagrees with CALL-TREE.md, CALL-TREE.md wins.
> User-facing bot text is Russian (product decision); code, configs and
> docs are English.

---

## 1. What this is

A personal **autonomous software-delivery factory**: a task goes in — a
Telegram message or a `spec.json` dropped into `tasks/inbox/` — and a staged
agent pipeline carries it to a GitHub pull request on the target repository.
One human gate: the merge.

The framework runs from this repo on macOS (`ops/atlas/aidstack.sh`: three
daemons) or Linux (`ops/systemd/` units). Multiple tasks
run in parallel (default cap 3), each in its own runner process and its own
git worktree of the target.

**Non-goals:** multi-tenant SaaS (one owner, one operator); IDE replacement
(the framework drives the `claude` CLI, it does not reimplement editors);
free-form chat (the Telegram surface is task-shaped); maximal autonomy —
the human merge gate is a feature, not debt.

---

## 2. Design tenets

These are the load-bearing decisions. Each is either validated by outside
research (see `research/roadmap-2026-08.md`) or bought by a recorded
incident (see `docs/PROVENANCE.md`); none is aesthetic.

1. **Sequential single-writer pipeline, parallel tasks.** Within one task
   exactly one stage writes at a time. Parallel *tasks* are safe (worktree
   isolation exists for this); parallel *co-writers inside one task* are a
   documented failure class (MAST/MAD) and are deliberately not built.

2. **Stages execute inside the target project, not the framework**
   (2026-08-15). A stage's `cwd` is the target repo (or its worktree), so it
   reads the *target's* `CLAUDE.md`/`AGENTS.md` — not this repo's push
   policy. Personas travel with the process via `--agents`
   (`dispatcher/agent_roster.py`); the CLI config is pipeline-owned and
   isolated from the operator's (`dispatcher/pipeline_config.py`).

3. **Review is adversarial and context-clean.** The reviewer stage runs
   three independent lenses (blind-hunter / edge-case-hunter /
   verification-gap, adapted from BMAD) with no shared context, then acts as
   the single severity authority over their findings. Reviewer and security
   personas are tool-locked to `Read, Grep, Glob` — physically unable to
   patch, enforced at the tool layer, not the prompt layer.

4. **Cheap where mechanical, strong where thinking.** Mechanical stages —
   execution of an already-written plan — route to a cheap
   Anthropic-compatible backend (`DEEPSEEK_STAGES`, currently
   `developer, developer-hotfix, tester, security`). Thinking stages
   (discovery/ba/pattern-detector/architect/tasks/analyze/edge-cases) and
   the reviewer stay on Anthropic. Two incident-bred guards temper the
   savings: a stage that failed review twice escalates to Anthropic for the
   rest of the task, and L-tier tasks take their first build/verify pass on
   Anthropic (a DeepSeek timeout once killed a live L-stage).

5. **Everything that can drift is generated or data.** Topology facts are
   generated and gated (CALL-TREE); prices are data
   (`BACKEND_PRICES_JSON`); stage prompts, artifact names and done-markers
   are data (`dispatcher/stage_prompts.py`); personas are files with
   recorded upstream provenance (`.claude/agents/UPSTREAM`).

6. **Degrade toward progress, park toward safety.** Memory infra down →
   the stage gets its plain prompt, never blocks. Triage LLM call fails →
   deterministic classification. Cost cap hit → the task *parks* in
   `awaiting-input` instead of dying. Limit storm → park and resume, not a
   timeout. A resumed runner restores its branch/PR lock (losing it once
   killed a fully green task after $14.56).

7. **Incidents become permanent mechanisms.** Orphaned `claude` children
   burning the subscription → `proc_reaper` process groups + sweep (#18).
   85 inherited env vars in stage children → allowlisted child env (#13).
   Notification spam → `notify_policy` (#19). The mechanism stays after the
   incident; removal requires data, not mood.

---

## 3. The shape

```
Telegram (bot/bot.py, RU surface) ──┐        its ONLY pipeline interface:
operator / cron writes spec.json ───┴──→ tasks/inbox/<id>/spec.json
                                              │
                     task_dispatcher.py  (queue owner; ≤ DISPATCHER_MAX_STAGES runners)
                                              │
                     stage_runner_agent.py  (one process per task)
                        ├─ triage: S/M/L sizing, deterministic-first
                        ├─ git worktree of the target repo
                        ├─ per stage: backend env → memory inject → claude -p
                        │             (cwd = TARGET, personas via --agents)
                        └─ draft PR + findings comments + Telegram status
                                              │
                     watcher.py  (crash respawn, limit-park resume, PR reconciliation)
                     proc_reaper.py  (process groups; orphan sweep on stack down)
```

The full tree with per-node "why it exists" is
[docs/CALL-TREE.md](docs/CALL-TREE.md); module-by-module roles are listed
there too.

---

## 4. The pipeline

Full stage sequence (the L route; several early stages are opt-in flags):

```
discovery → ba → pattern-detector → architect → tasks → analyze →
edge-cases → developer → tester ∥ security → reviewer
                └────────── developer-hotfix loop on findings ──────────┘
```

- **Triage** runs first and sizes the route: S drops all upstream reasoning
  stages, M keeps BA, L runs everything. The
  developer/tester/security/reviewer core never drops. Risk signals are
  deterministic and dominate; underestimation upgrades eagerly (S→M→L).
- **Artifacts + resume.** Every stage writes its artifact
  (`01-ba-agent.md` … `06-review-agent.md`) in the task dir; an existing
  artifact means the stage is skipped on respawn. This one rule is what
  makes crash recovery, limit parking and backend rotation all work.
- **Gates between stages:** INVEST check on the BA artifact, deterministic
  architecture lint on the Architect artifact, a clarification pause on
  ambiguous specs, and the budget gate on every stage boundary.
- **Verdict loop.** The reviewer's verdict drives hotfix iterations under
  an iteration cap; unresolved findings are posted to the PR as comments.

---

## 5. Backends and honest cost

Stages run against Anthropic-compatible endpoints, switched per stage by
environment rewrite (`dispatcher/backend_routing.py`): `anthropic`,
`deepseek`, `glm`. Routing is env-data (`DEEPSEEK_STAGES`), overridable per
task via `spec.json:model_routing`, and tempered by the two guards from
tenet 4.

The `claude` CLI prices every session at Anthropic rates regardless of
endpoint (~22× off for DeepSeek), so for non-anthropic backends
`apply_backend_pricing` recomputes the stage cost from the CLI's true token
counts × the provider's price table. Prices are data: built-in reference
table + `BACKEND_PRICES_JSON` override (kept at *peak* rates as the honest
upper bound since DeepSeek's 2026-08-17 peak/off-peak split). Every figure
carries its source label (`cli` / `computed:<model>` /
`cli-no-price-table:<backend>`), lands in both the artifact and the
append-only SQLite `cost_ledger` — one write point, so they cannot diverge.
The budget gate reads the honest number; on cap it parks the task for an
operator decision instead of killing it.

---

## 6. Memory (four layers)

| Layer | Scope | Managed by |
|---|---|---|
| `memory-bank/` in each target repo | per-project facts (goal, stack, decisions) | git; post-merge auto-append of a one-liner |
| Pipeline memory (flat JSONL + TEI) | cross-task lessons, typed + scoped | the runner, `dispatcher/memory_inject.py` |
| Claude Code auto-memory | operator-session behavior | the Claude Code harness |
| `STATE/` (private) | this repo's own development | by hand, every meaningful change |

The pipeline layer is the working one (memory-inject, roadmap #0, live
since 2026-08-15). Both halves run inside the runner, with no new
dependencies. The store behind them is a single JSONL file —
`memory-bank/semantic-export/meta_agent_mem.vectors.jsonl`, one
`{id, vector, payload}` record per line, scanned with a stdlib cosine
top-k (`dispatcher/memory_flat.py`, `MEMORY_FLAT_ENABLED=1`). The one
service still required is TEI `bge-m3` :8087, over stdlib HTTP: the query
has to be embedded. Qdrant was the store until 2026-08-21 (T13 verdict:
600 MB and two always-on services for 810 points, ~40 GB/day of idle disk
reads); with the flag off the Qdrant path is still there, kept as the
rollback.

- **Write-back:** every completed pipeline writes one typed `task_lesson`
  record (`{kind, target_repo, tier, stop_reason, pr_url, summary}`).
  Typing is what makes scoping possible. A per-target cap (200, oldest
  retire) guards against retrieval dilution.
- **Inject:** before the ba/architect/developer prompts are built, the
  `<injected-memory>` slot is filled with this target's typed records
  first, then global semantic hits from the legacy store. Any infra
  failure degrades to `(none)` — a stage never blocks on memory.

The predecessor — four mem0 lifecycle hooks in `.claude/settings.json` —
was removed 2026-08-17 after months dead; the points it once wrote came
across in the migration and are served by the global recall. The bot's
`/memo` and `/recall` commands are a manual layer over the same store,
through `memory_inject` rather than a store of their own.

---

## 7. Isolation and safety rails

- **Worktree isolation** — implementation stages work in an ephemeral
  `git worktree`; a self-targeted task cannot yank the live deployment's
  files from under itself, and parallel tasks never fight over a checkout.
- **Allowlisted child env** (#13) — stage children run with
  `--dangerously-skip-permissions`, so the env *is* the attack surface:
  children get base POSIX vars, harness vars, and the routed backend's
  key family only. A DeepSeek stage never sees `GLM_API_KEY`; no stage
  sees the bot token.
- **Pipeline-owned `CLAUDE_CONFIG_DIR`** — stages authenticate from an
  isolated config (macOS keychain seeding included), not the operator's.
- **Process-group ownership** (#18) — every `claude` child is spawned into
  its own group via `proc_reaper`; stack shutdown sweeps orphans. The
  matcher can never select an interactive tty session.
- **Limit handling** (#11) — limit storms detected on the live stream park
  the task; the watcher resumes it when the window reopens.
- **PoC seatbelt** — targets outside `MERGEABLE_REPO_PATHS` (in `bot/.env`)
  run in PoC mode: PRs are opened but never merged by the pipeline.
- **Secrets** — never flow through prompts: env-injection only, log
  redaction on the bot, gitleaks in CI on every push and pull request, plus
  the two-scan export gate `scripts/publish-public.sh` (gitleaks + project
  blocklist) that anything would have to pass to reach the public mirror.
  Publication is paused since 2026-08-21; the mirror is live, so the rule
  "never push there" stands.

---

## 8. Task lifecycle

```
tasks/inbox/      spec.json (schema-validated; telegram trigger requires a thread ref)
tasks/active/     state.json + worklog.md + per-stage artifacts + .runner.pid
tasks/awaiting-input/     parked: cost cap, limit storm, clarification — operator decides
tasks/awaiting-approval/  reviewer approved; PR open, merge is the human gate
tasks/done/  tasks/failed/  terminal (failed keeps a .reason.txt post-mortem)
```

The dispatcher is the queue's only mutator; the bot and cron only ever
write inbox specs. `state.json` carries per-stage costs, iteration count,
triage tier and the telegram thread; `worklog.md` is the human-readable
trace (memory injections and write-backs log there too).

---

## 9. Deploy and operations

- **macOS (current):** `ops/atlas/aidstack.sh` up/down/status/logs (shell
  aliases `aidup`/`aiddown`/`aidstatus`/`aidlogs`). Up: dispatcher +
  watcher + bot, each sourcing `bot/.env` (plus the Qdrant container only
  when `MEMORY_FLAT_ENABLED` is off). Down: daemons, then the orphan
  sweep.
- **Linux:** the same three daemons as `ops/systemd/` units;
  `ops/INSTALL.md` is the full walkthrough.
- **Optional stacks** under `services/stacks/`: voice (Whisper STT +
  Silero TTS) and Windmill (scheduling); the pipeline itself needs only
  bot + dispatcher + watcher + `claude` + `gh` + TEI.

---

## 10. Where truth lives

1. The code (`git log`).
2. [docs/CALL-TREE.md](docs/CALL-TREE.md) — generated facts, suite-gated.
3. This file — intent and tenets; update it when a *decision* changes.
4. `STATE/` + `docs/PROVENANCE.md` — direction and origins.

The update ritual is mechanical: change the topology → the suite fails →
`ops/check-arch-map.py --update` → fix any prose the diff falsified — in
CALL-TREE first, here only if a tenet moved.
