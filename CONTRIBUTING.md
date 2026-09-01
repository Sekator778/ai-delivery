# CONTRIBUTING — How to use and contribute to ai-delivery

Two audiences in one document:
- **Operator** (Part 1) — you have the repo installed and want to use the bot
- **Contributor** (Part 2 + 3) — you want to change the framework itself

If you're both — read in order.

---

## Part 1 — Using the framework (operator)

### Installation

Fresh host: follow `ops/INSTALL.md` step-by-step (WSL2 dev path: steps 1-10; Linux server path: S1-S7 + the new S5b for LiteLLM proxy if you want it). One-time setup; everything is systemd-managed after.

### Daily flow

```
You (Telegram)                ai-delivery bot                    target repo
   │                                │                                │
   ├─ /task @userbot <prompt> ──→   │                                │
   │                                ├─ writes tasks/inbox/<id>/      │
   │                                ├─ task_dispatcher picks up      │
   │                                ├─ stage_runner runs:            │
   │                                │   BA → Architect → Developer ──┼─→ opens PR
   │                                │   ↓                            │
   │                                │   Tester ‖ Security            │
   │                                │   ↓                            │
   │                                │   Reviewer                     │
   │                                ├─ APPROVE? → Telegram [Да/Нет]  │
   │                                │                                │
   ├─ tap [Да] ──────────────────→  │                                │
   │                                ├─ gh pr merge --squash  ────────┼─→ merged
   │                                ├─ updates memory-bank in target │
   │                                ├─ moves task to tasks/done/     │
   │                                └─ ✓ Merged. PR: <url>           │
```

### Command surface (Telegram)

| Command | What it does |
|---------|--------------|
| `/task @<alias> <text>` | Submit a task to the named project's pipeline |
| `/main <text>` | Direct meta-Claude channel (framework dev, bypasses pipeline) |
| `/projects` | List configured project aliases |
| `/usage [today\|week\|month\|all]` | Cost report (via `npx ccusage`) |
| `/memo <fact>` | Store long-term memory (FastEmbed + Qdrant) |
| `/recall <query>` | Semantic search over /memo facts |
| `/schedule` | Recurring task setup |
| `/help` | Command reference inside Telegram |

Voice messages route to `/main` (Whisper STT → text → handle).

### When something breaks

`ops/runbook.md` is the incident playbook. Most failures are:
- Rate-limit on current backend → bot sends inline keyboard with switch options
- Task stuck → check `tasks/awaiting-input/<id>/state.json`, `worklog.md`
- Service down → `sudo systemctl status claude-tg-bot task-dispatcher watcher`

### Backend switching

When the current backend hits rate-limit, the bot sends a Telegram inline keyboard with "Switch to deepseek" / "Switch to glm" / "Wait N min". One tap flips routing for the next task. If GLM is configured (`GLM_API_KEY` in `bot/.env`), it appears in the menu.

Auto-escalation (per `STATE/DECISIONS.md` → `auto-escalate-on-stalled-cheap-backend`): at iteration ≥ 2 of a task's hotfix loop, non-anthropic Dev/Test/Sec stages auto-flip to anthropic for the rest of the task. Per-task reset — next task starts on the default routing.

### Optional features (opt-in via env)

| Env var | Effect when set |
|---------|-----------------|
| `LITELLM_PROXY_URL=http://localhost:4000/v1` + `LITELLM_MASTER_KEY=<key>` | DeepSeek/GLM traffic routes through LiteLLM proxy (cooldown + fallback chain). Max OAuth stays direct. See `ops/litellm/README.md` |
| `DISCOVERY_ENABLED=1` | Adds a Discovery stage before BA: surfaces relevant existing context (file:line citations, ADRs, patterns) to `00-discovery.md`; BA reads it |
| `PATTERN_DETECTION_ENABLED=0` | (agent-path) Opt OUT of the pattern-detector stage — ON by default; feeds existing-pattern constraints to the Architect (`01b-patterns.md`) |
| `TASKS_STAGE_ENABLED=1` | (agent-path) Adds a Spec-Kit `/tasks` stage after Architect: emits a dependency-ordered `[P]`-marked `02b-tasks.md` the Developer executes phase-by-phase |
| `ANALYZE_STAGE_ENABLED=1` | (agent-path) Adds a Spec-Kit `/analyze` cross-artifact consistency stage after `tasks` (`02c-analyze.md`). Requires `TASKS_STAGE_ENABLED` |
| `ANALYZE_GATE_BLOCKING=1` | Makes the analyze stage's CRITICAL findings hard-fail the task (default: report-only) |
| `EDGE_CASES_STAGE_ENABLED=1` | (agent-path) Adds a BMAD Edge Case Hunter stage before Developer (`02d-edgecases.md`); Developer adds a guard + test per finding |
| `BA_QUALITY_GATE_ENABLED=0` | (agent-path) Opt OUT of the BA quality hard-gate (Spec-Kit checklist + no live `[NEEDS CLARIFICATION:]` before Architect; ON by default) |
| `SPECS_FOLDER_MIRROR_ENABLED=1` | (agent-path) Additively mirror `01-ba.md`/`02-architecture.md`/`02b-tasks.md` into a Spec-Kit `specs/{spec,plan,tasks}.md` folder. Purely additive — the flat names stay primary; nothing reads the mirror yet (alias-staging phase 1 of the folder migration). |
| `INVEST_VALIDATION_ENABLED=1` | (agent-path) Run INVEST validation of the BA artifact; blocks on violations unless `INVEST_BLOCKING=0` |
| `STAGE_RUNNER_MODE=agent` | (planned — Phase C) routes the pipeline through `dispatcher/stage_runner_agent.py` instead of subprocess. Tool restrictions from `.claude/agents/*.md` are enforced |
| `MEMORY_FLAT_ENABLED=1` | Recall and write-back go to a JSONL flat store (`MEMORY_FLAT_PATH`, default `memory-bank/semantic-export/meta_agent_mem.vectors.jsonl`) instead of Qdrant — same ranking, no database. Produce the file first with `scripts/qdrant-memory.py dump --with-vectors`; TEI is still required (the query must be embedded). `0` (default) = Qdrant, unchanged |
| `EGRESS_SCOPING_ENABLED=1` | Confine stage children to an allowlist of network domains, enforced by the CLI's own sandbox (local proxy + kernel backstop) rather than by env hygiene alone. `0` (default) = off, byte-identical settings. Widen the list per host with `EGRESS_EXTRA_DOMAINS=host1,host2`. Requires a smoke run on a sandbox target before trusting it — see `STATE/DESIGN-2026-08-21-egress-scoping.md` |
| `CLARIFY_DEADMAN_HOURS=6` | Clarify dead man: a task parked on BA's `[NEEDS CLARIFICATION]` questions for this many hours is resumed ONCE on the defaults the BRD already records for those markers (worklog + Telegram say so). `0` (default) = off, the pause waits for a human forever. A second clarify pause on the same task always waits for a human |
| `STAGE_ESCALATION_AT_ITERATION=999` | Disable auto-escalation (default 2) |
| `CC_LANGSMITH_API_KEY=ls__...` | LangSmith traces light up for every stage |

Edit `bot/.env`, restart services. Don't edit `bot/.env.example` (template only — committed).

### Provider key profiles

One provider can have several keys — a personal one, a work one, someone else's
quota. `bot/providers.json` (gitignored; copy `bot/providers.example.json`)
gives each a name:

```json
{"profiles": {"deepseek-alt": {"backend": "deepseek",
                               "api_key_env": "DEEPSEEK_API_KEY_ALT"}},
 "defaults": {"deepseek": "deepseek-main"}}
```

The file holds **no secrets**: a profile points at an environment variable
(`api_key_env`, filled from `bot/.env`) or a file (`api_key_file`). Which
profile pays is decided per task — a `model_routing` value may name it after a
colon, `"deepseek:alt"` — or per session with `/backend deepseek:alt` in
Telegram, which stamps `provider_profile` into every new spec. Neither given:
`defaults.<backend>`. **No registry at all: nothing changes** — stages use the
global `DEEPSEEK_API_KEY` / `GLM_API_KEY` exactly as before.

The profile name lands in `state.json`, in the cost ledger and in
`ops/cost-report.py`'s "By key profile" slice, so spend can be split per quota.
The key value never does: the parent resolves it and writes only
`ANTHROPIC_AUTH_TOKEN` into the child, and a child running on a profile does
not get the provider's default key either.

---

## Part 2 — Contributing (developer)

### Before you write code

1. Read `STATE/CURRENT.md` — what's running, what's in flight, where you'd insert your change
2. Read `STATE/DECISIONS.md` — the ADR log. Big — but reading the headlines (search for `### `) gives you the lay of the land
3. Skim `STATE/ROADMAP.md` — phase status
4. Skim `ARCHITECTURE.md` — modules + contracts (only sections relevant to your area)

### The size-of-change ladder

| Tier | Time | Pattern | Examples |
|---|---|---|---|
| **1** | 5-30 min | Single-file fix. Conventional commit. Push. | env var addition, regex fix, typo, copy update |
| **2** | 2-3 days | Swap bespoke code for community standard. Phase 1 (infra) + Phase 2 (wiring) separate commits. Opt-in env flag mandatory. | LiteLLM proxy, `/usage` → ccusage, FastEmbed + Qdrant for `/memo` |
| **3** | 1-2 weeks | Architectural. Phased rollout MANDATORY (A: PoC → B: extend → C: flip default → D: remove old path). | stage_runner subprocess → Agent tool |
| **4** | optional | Nice-to-have. Document in ROADMAP as candidate. Pick up when other tiers settle. | Pattern-Detection subagent, Spec-Kit interactive /clarify |

Don't skip phases. Tier 3 Phase A on the lowest-risk surface is non-negotiable.

### Commit hygiene

- **Conventional Commits required**: `feat:` / `fix:` / `docs:` / `refactor:` / `chore:` / `test:`
- **Scope after type**: `feat(dispatcher): ...`, `docs(state): ...`
- **Subject line ≤ 70 chars**, English only
- **NO `Co-Authored-By:` footer** — we don't attribute generated code that way
- **NO "Generated with Claude" / "🤖" / AI-attribution lines**
- **Body explains WHY**, not what (the diff shows the what)

### Two safety patterns you must internalize

**1. The opt-in feature pattern** — every new infrastructure addition is gated by an env flag, default OFF. Existing pipeline behaves identically until you opt in. This is the canonical safety lever.

Examples in the codebase:
- `LITELLM_PROXY_URL` — proxy off by default
- `DISCOVERY_ENABLED` — Discovery stage off by default
- `STAGE_ESCALATION_AT_ITERATION=999` — disable auto-escalation

**2. The PoC safety pattern** — when a new code path WRITES to git / target repo / external systems for the first time, enforce constraints in the verdict parser (not just the prompt):

- Branch names prefixed with `phase-<N>-poc-<UTC-timestamp>`
- PR titles prefixed with `[PoC, DO NOT MERGE]`
- Conventional commits, no AI-attribution
- Orchestrator NEVER pushes to origin/main or merges anything

Canonical example: `dispatcher/stage_runner_agent.py:STAGE_PROMPTS["developer"]` — verdict parser rejects branches without `phase-b4-poc-` prefix with `safety_violation` exit code.

### Adding or changing a pipeline stage

There is one runner — `dispatcher/stage_runner_agent.py` (the subprocess path was
removed in Phase D). To change a stage prompt, update
`stage_runner_agent.py:STAGE_PROMPTS[stage]` — the INNER subagent prompt inside the
orchestrator wrap; keep the outer wrap intact.

To add a new stage, update the stage maps in `stage_runner_agent.py`
(`STAGE_AGENT_MAP`, `STAGE_ARTIFACT_MAP`, `STAGE_PROMPTS`, `_build_format_kwargs`,
the verdict-parsing dispatch, `STAGE_DONE_MARKERS`) and the
`dispatcher/schema/spec.schema.json:model_routing` enum.

### Delegation rules (the deepseek BG rule)

**Mechanical work → claude-deepseek as background agent.**
**Design / architecture / debugging → main Opus session.**

The rule, codified after an early incident: dispatch small, independent tasks to the cheaper backends **in parallel**; reserve the main Opus session for design, architecture, and debugging.

Pattern:
```bash
ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
ANTHROPIC_AUTH_TOKEN="$DEEPSEEK_API_KEY" \
ANTHROPIC_MODEL="deepseek-v4-pro" \
claude --dangerously-skip-permissions -p "$(cat /tmp/spec.md)" &
```

In Claude Code itself: `Bash` tool with `run_in_background: true` and the env-prefixed `claude` invocation. Fire multiple BG agents in one response when the work is independent (e.g., parallel Phase B sub-step dispatches).

**Sharp specs only**:
- Imperative ("Create file X with body Y", not "consider adding X")
- File paths absolute
- Exact strings to find/replace
- Verification commands the agent must run before commit
- Commit message format
- "DO NOT" list (out-of-scope items)

Vague specs trigger clarifying-question loops and timeouts.

### Phased rollout — canonical Tier 3.1 example

| Phase | Time | What |
|---|---|---|
| **A** | 1-2 days | PoC `dispatcher/stage_runner_agent.py` wires ONE stage (Reviewer — read-only, lowest blast radius). Manual harness, not integrated with `task_dispatcher`. Validate end-to-end on a real task; compare against subprocess output |
| **B** | 1 week | Extend to remaining 6 stages, atomic commit per sub-step (B1, B2, ...). After each: harness still works for previously-wired stages, regression-tested |
| **C** | 1 day work + 1 week observation | `STAGE_RUNNER_MODE=agent` plumbed into `task_dispatcher.py`. Default OFF. Set to ON after manual smoke tests pass. Observe real traffic for a week |
| **D** | 1 day | Remove subprocess path. Rename Agent-tool path to canonical name. Delete dead code (~200 lines for Tier 3.1: `_subagent_env`, `_detect_rate_limit`, etc.) |

Time investment ~2-3 weeks. Risk minimized at each phase. Rollback at any phase is `git revert`. **(Tier 3.1 completed 2026-06-03 — Phase D removed the subprocess path; this table is kept as a worked example of the phased-rollout discipline.)**

### Where things live

```
dispatcher/             pipeline state machine
  stage_runner_agent.py      Agent-tool execution — the only runner (all stages wired)
  task_dispatcher.py         inbox → active → spawn stage_runner_agent subprocess
  watcher.py                 crash recovery, respawn orphaned runners
  auto_loop.py               REQUEST_CHANGES → hotfix loop (cap=3 iterations)
  cost_ledger.py             SQLite per-stage cost (survives task cleanup)
  schema/spec.schema.json    spec.json contract (validates inbox tasks)
bot/                    Telegram bot
  bot.py                     handlers + HTTP /notify endpoint
  start.sh                   venv + .env loader (systemd ExecStart)
ops/                    operator guides
  INSTALL.md                 fresh install S1-S7
  runbook.md                 incidents
  self-healing.md            watcher/respawn design
  litellm/                   proxy infra (Phase 1 of LiteLLM)
.claude/                Claude Code session config
  agents/                    16-agent catalog (per-stage subagents, tool restrictions)
  commands/                  slash commands (team-spawn, full-review, etc.)
STATE/                  recovery layer (this is the "memory of the machine")
  CURRENT.md                 what's running NOW (read at session start)
  ROADMAP.md                 phase status
  DECISIONS.md               ADR log, append-only
  ARCH-REVIEW-*.md           independent architecture critiques
  evaluations/               tool/library evaluation reports (BMAD, etc.)
  poc-results/               PoC validation evidence dumps
memory-bank/            (per-target-project memory — lives in TARGET repo, not here)
tasks/                  task lifecycle directories
  inbox/                     just-arrived from Telegram
  active/                    being processed by a stage_runner
  awaiting-approval/         Reviewer APPROVE'd; Telegram inline keyboard pending
  awaiting-input/            escalated (iteration cap, watchdog, cost cap, stagnation)
  done/                      after the user tapped Да
  failed/                    terminal failure
```

### Running the suite, and what CI does

```bash
bot/venv/bin/python -m unittest discover -s tests      # the suite
python3 -m pip install jsonschema                       # fresh clone: the one missing dep
```

**Not pytest.** Two pytest-style files are not collected by `unittest
discover`, and every baseline this project quotes is a `discover` number.

`.github/workflows/ci.yml` runs the same command on every pull request and on
pushes to `dev`, across Python 3.10 (the documented floor) and 3.12 (what atlas
runs), plus a `bash -n` pass over every tracked shell script and a `gitleaks`
scan of the working tree and of the commits the change adds.

Two things it deliberately does **not** do, so a green tick is not read as more
than it is:

- **No build or deploy.** There is no build artifact here, and delivery is the
  pipeline's own Tester stage plus the operator's smoke gate.
- **No full-history secret scan.** History carries six known findings from
  before the 2026-05-27 key rotation; scanning it would be red forever. CI
  scans what a change *adds*.

CI needs `gitleaks` on `PATH` (without it the `test_publish_public` tests fail)
and a full clone with tags (`scripts/publish-public.sh` refuses to run without
a reachable version tag). If you reproduce a CI failure locally, match those.
Those tests cover a script whose use is **paused**: the mirror is live but no
longer refreshed since 2026-08-21 (`CLAUDE.md` §1). They keep running — the
export filter they exercise is what stands between an internal directory and a
live public repository.

### Pre-flight checklist before merge

- [ ] Tests pass (or explicitly stated why skipped — usually never)
- [ ] CI green on the PR — and if a job is red, fixed rather than re-run
- [ ] STATE/CURRENT.md updated if state shifted
- [ ] STATE/DECISIONS.md has an ADR for any non-trivial decision (yes, even short ones)
- [ ] No `Co-Authored-By:` or AI-attribution footer in commit
- [ ] If introducing new infrastructure: opt-in env flag, zero regression by default
- [ ] If writing to target repo / git: PoC safety constraints enforced in code (not just prompt)
- [ ] If touching a stage prompt: both `SYSTEM_PROMPTS` and `STAGE_PROMPTS` paths updated
- [ ] Bot still imports cleanly: `source bot/venv/bin/activate && python3 -c "import sys; sys.path.insert(0, 'bot'); import bot"`
- [ ] Dispatcher still imports cleanly: `python3 -c "import sys; sys.path.insert(0, 'dispatcher'); import stage_runner; import stage_runner_agent"`

---

## Part 3 — Rituals

Recurring patterns of work that keep the project's memory and state coherent.

### Per Claude-session ritual

- **Start of session** — STATE/CURRENT.md is auto-loaded by Claude Code project context (no action needed; just know it's there)
- **During work** — when state shifts meaningfully (a stage completed, a phase moved, a decision made), update STATE/CURRENT.md inline. Don't batch — the recovery layer must be current
- **End of session** — STATE/CURRENT.md reflects exact state for the next session's resume point. If a session crashes, the next one reads CURRENT.md and continues without context loss

### Branch model

Two long-lived branches, and each has one job.

**`dev` is where development happens.** Every change lands there, through a
pull request. It is what a working clone sits on.

**`master` is what the outside world starts from.** It is the repository's
default branch on GitHub, so it is what a fresh clone gets, what a new harness
session is cut from, and what anything triggered against this repo runs. That
is the whole reason it exists — not an archive of past releases.

Which gives the one rule that matters: **`master` must not go stale.** A
trigger branch that lags behind `dev` runs old code and reports on it
confidently. Merge `dev` into `master` once work has landed and CI is green —
at a milestone, at a release, and in any case before anything is triggered from
`master`. Waiting for a version tag is not a reason to leave it behind.

| Branch | What it is |
|---|---|
| `master` | GitHub default; what clones, sessions and triggers start from. Kept current by merging `dev` into it. Never committed to directly. |
| `dev` | where development happens; base for every pull request |
| `feat/*`, `fix/*`, `chore/*` | short-lived work branches, one change each |
| `claude/*` | the same, cut by a Claude Code harness session |
| `archive/*` | frozen history kept on purpose — never delete, never build on |

Rules that follow from it:

- **Base every PR on `dev`.** GitHub preselects `master`, because `master` is
  the default on purpose — retarget the PR. That retarget is the price of
  having one branch that triggers cleanly, and it is a two-second edit in the
  PR header.
- **Nothing is committed to `master` directly.** It only ever receives `dev`.
- **`dev → master` is fast-forward when it can be**, and a merge commit when it
  cannot. The fast-forward is the healthy case and means `master` carries
  exactly what `dev` does. If it refuses, find out what landed on `master` out
  of band before going further — but do not leave `master` stale over it.
- **A work branch dies with its PR.** Delete it after the merge (the repository
  has "automatically delete head branches" on; delete a stale one by hand with
  `git push origin --delete <branch>`). Before deleting anything by hand, prove
  it is merged: `git merge-base --is-ancestor origin/<branch> origin/dev`. A
  branch that fails that check still carries work — check what it is before
  removing it, and never delete `archive/*`.
- **Merge style into `dev`:** pull requests land as **merge commits**, not
  squashes — the individual commit messages are this project's record of *why*,
  and squashing throws that away.
- **Hotfixes** still go through `dev`, then straight into `master`. Branching
  off `master` is for the case where `dev` carries work that must not ship yet.

Tagging a version is a separate, smaller thing: it names a state of the tree
worth returning to. Checklist: [ops/RELEASE.md](ops/RELEASE.md).

### Per-feature ritual

1. **Before** — write an ADR in STATE/DECISIONS.md. Even short. The decision + the why + how to apply.
2. **During** — phased commits (small, atomic, conventional). Opt-in env flag if introducing infra.
3. **After** — STATE/CURRENT.md updated. STATE/poc-results/ populated if PoC validated. STATE/ROADMAP.md adjusted if phase status changed.

### Per pipeline-task ritual (the user's perspective)

1. `/task @<alias> <text>` in Telegram
2. Watch the stage updates (one Telegram message per stage start + completion + cost)
3. Wait for `✓ APPROVE. PR: ... [Да] [Нет]` (or escalation menu if it stalled)
4. Tap [Да] → automatic merge + memory-bank auto-update + task moves to done/

If the task escalates to awaiting-input/, read the worklog and `06-review.md`, decide whether to retry / hotfix manually / abandon.

### Periodic rituals (informally)

- **Weekly** — review STATE/ROADMAP.md against actual progress. Prune stale candidates. Note new ones.
- **On compass-research result** — STATE/evaluations/<thing>-YYYY-MM-DD.md so the decision and its source land in git
- **On major handoff** (licensing review, etc.) — refresh `C:\Users\user\Desktop\ai-delivery-review-<date>\` package by copying STATE/ + ops/ + README + ARCHITECTURE and writing summary docs

### The memory layers (when to write to which)

Four memory layers (see ARCHITECTURE.md §6):

| Layer | What goes here | Who writes |
|---|---|---|
| **memory-bank/** (in target repo) | Per-project: what exists, decisions, tech stack | BA + Architect read; Block 4.2 auto-appends `Recent merged changes` |
| **Qdrant `meta_agent_mem`** | Cross-task semantic facts auto-captured by hooks | Stop / SubagentStop / PreCompact / UserPromptSubmit hooks |
| **`~/.claude/projects/-.../memory/`** | Feedback rules, project context for Claude Code | You, via auto-memory tool calls |
| **STATE/** (in ai-delivery repo) | Recovery layer, ADR log, roadmap | You, explicitly, in commits |

Rule of thumb: **STATE/ is for things you'd lose if the session crashed**. Qdrant + auto-memory are for things you'd lose between sessions. memory-bank/ is for things the BA stage needs to read at every task start.

---

## Quick reference card

```
First time setup            → ops/INSTALL.md
Daily ops + incidents       → ops/runbook.md
Architecture overview       → ARCHITECTURE.md
Current state               → STATE/CURRENT.md
Why we did X                → STATE/DECISIONS.md
What's planned              → STATE/ROADMAP.md
Tool evaluations            → STATE/evaluations/
PoC evidence                → STATE/poc-results/
Telegram command reference  → bot.py `_help_text()` / Part 1 above
WSL2 gotchas                → ops/WSL2-NOTES.md
Self-healing design         → ops/self-healing.md
```
