# ai-delivery

**A self-hosted control-plane and reliability layer over a frontier coding runtime.**

One Telegram message becomes a full SDLC pipeline that opens a real GitHub pull
request — with a human at the merge gate. It does **not** try to out-code the
model: it wraps Claude Code (with DeepSeek/GLM failover) and owns the part that
actually decides quality and cost — the **control loop** around the runtime.

```
Telegram / cron ─▶ discovery ─▶ BA ─▶ pattern-detector ─▶ architect ─▶ developer
                                                                          │
                       PR ◀── reviewer ◀── tester ‖ security ◀───────────┘
                        │
                   human merge gate
```

Runs on a single Linux host under systemd. `git clone` + the install script gives
you a working system — code, configs, and operator guide all live here.

![Demo: one task in, a self-running pipeline and a reviewed PR out](docs/demo.gif)

*One request in; the pipeline runs itself (per-stage wall-time + cost) and opens a
reviewed PR that stops at the human merge gate.*

> **What it is:** orchestration + governance + reliability over a coding agent.
> **What it isn't:** a new agent runtime, or an autonomous "replace your team" tool.
> The capability ceiling is the wrapped model; the contribution is everything
> *around* it — routing, stop conditions, cost governance, crash recovery,
> human-in-the-loop merge.

## Design under failure

The interesting engineering isn't the happy path — it's what the harness does when
things go wrong. Each policy below was driven by a real failure on real runs (full
write-up: *failure-driven design in an autonomous delivery agent*). Every fix is a
rule in the control loop, not a better prompt:

| Failure mode | Policy (where it lives) |
|---|---|
| Double-spawn race (two runners, one task) | Authoritative `flock` per task — `stage_runner_agent.py:_acquire_runner_lock`; kernel frees it on death |
| Nitpick loop ($15.98 for a one-liner) | 0-critical `request_changes` → approve, findings as a PR comment — `post_pipeline.py:_decide_post_pipeline_stage` |
| Non-convergence blowout | Two knobs, not one: stop-early vs keep-fixing — `control_loop.py:_critical_is_converging` (strictly-decreasing trend) |
| Timeout ≠ crash | Distinct `RC_STAGE_TIMEOUT`, no retry-burn, graceful handoff with partial work preserved — `:_handoff_terminal` |
| Flaky LLM verdict across a human pause | Persist the decision: durable `triage.json` (`TRIAGE_STICKY`) — `triage_wiring.py:_persist_triage` |
| Unit-green ≠ deploy-green | Ephemeral deploy-smoke stage — run the harness for real |
| Orphaned task states | Every die-state has an owner — `watcher.py` adopts/reconciles orphans |

**The thesis:** the control loop is the product. The model writes the code; the
harness decides when to stop, retry, escalate, hand off, and what it costs.

## Architecture at a glance

The orchestrator is decomposed into focused, single-responsibility modules behind
a stable façade (a deliberate split from a former 4089-line monolith → 1908,
−53%, with the full suite green at every step):

| Module (`dispatcher/`) | Responsibility | LOC |
|---|---|---|
| `stage_runner_agent.py` | the orchestrator: `run_pipeline`, stage exec, gates, handoff | 1908 |
| `stage_prompts.py` | per-stage prompts + stage maps (pure data) | 1167 |
| `backend_routing.py` | model/backend selection + subprocess env rewrite | 330 |
| `git_pr.py` | git/gh I/O: PR parse, recovery, draft PR, comments | 244 |
| `triage_wiring.py` | adaptive-complexity triage primitives | 183 |
| `control_loop.py` | convergence / anti-thrash / cost primitives | 144 |
| `post_pipeline.py` | reviewer-verdict → next-state decision (nitpick guard) | 141 |
| `target_policy.py` | target classification + branch-safety seatbelt | 106 |
| `runner_state.py` | worklog / history / state.json side effects | 57 |
| `telegram_io.py` | bot / Telegram notification side-channels | 57 |

~13.1k LOC first-party · 191 tests · MIT · single-host self-hostable.

## What's inside

| Path | Component |
|---|---|
| `bot/` | Telegram bot — text/voice intake, admin commands, inline-keyboard approvals |
| `dispatcher/` | File-queue daemon + stage-runner: ingests `tasks/inbox/`, drives the pipeline |
| `meta/` | Agent prompts and Claude CLI invocation wrappers |
| `bin/` | CLI helpers (`botctl-*`) |
| `tasks/` | Lifecycle: `inbox/` → `active/` → `awaiting-input\|approval/` → `done\|failed/` |
| `windmill/` | Windmill stack + flow templates for cron schedules |
| `services/stacks/` | Docker stacks: Qdrant (mem0), Whisper + Silero (voice STT/TTS) |
| `dispatcher/hooks/` | Claude Code lifecycle hooks — mem0 auto-capture + relevant-fact injection |
| `ops/` | Install scripts, systemd units, operator notes |
| `STATE/` | Live project state — read `STATE/CURRENT.md` first |

## Deploy on a fresh host

Full guide: **[ops/INSTALL.md](ops/INSTALL.md)**. Short version:

```bash
git clone https://github.com/Sekator778/ai-delivery.git ~/projects/ai-delivery
cd ~/projects/ai-delivery
# Install Docker, Node, Claude CLI, uv, Python deps — see ops/INSTALL.md steps 1-6.
cp bot/.env.example bot/.env && chmod 600 bot/.env
$EDITOR bot/.env          # TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID, provider keys
sudo ops/systemd/install.sh   # claude-tg-bot, task-dispatcher, watcher, windmill
systemctl status claude-tg-bot task-dispatcher watcher ai-delivery-windmill
```

## Operate

Administered over Telegram by the owner (`OWNER_TELEGRAM_ID`); everyone else is
rejected silently. Key inputs:

| Telegram input | Effect |
|---|---|
| `/task [@alias] <prompt>` (or free-form text / voice) | Creates `spec.json` in `tasks/inbox/`; `@alias` (see `bot/projects.json`) picks the target repo, else the `_default` |
| `/projects` | List registered project aliases |
| `/usage [today\|week\|all]` | Cost report from `tasks/*/state.json` — per-stage + per-backend |
| `/tasks`, `/requeue <id> [guidance]` | List parked tasks; resume a handed-off one from chat |
| approve / decline buttons after Reviewer APPROVE | Merge the PR (`gh pr merge --squash`) → `done/`, or leave it |

Safety: a target repo produces a real, mergeable PR **only** if it's explicitly
allowlisted (`MERGEABLE_REPO_PATHS`); every other target stays in PoC mode
(`phase-b4-poc-*` branch, `[PoC, DO NOT MERGE]` title) — fail-safe by default.

## Status

Validated on the author's own repos across S/M/L complexity tiers (a handful of
runs, not a benchmark matrix). No external SWE-bench number yet — the measured
outcome so far is cost, and the failures above are documented honestly. See
`STATE/CURRENT.md` for live development state.
