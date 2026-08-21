# Phase 0 — Infrastructure foundation. Execution plan (runbook)

The "executable" version of Phase 0 from `research/research_report.md` (Stage 4.1) — a detailed runbook.

**Goal of Phase 0:** a production-grade foundation on which the pipeline drives a task end-to-end
on **real models**, **real CI/CD**, and **Git/PR** — up to the approval gate.

## Who does what

- 🟦 **the team** — procurement / provisioning (accounts, server, access). This cannot be done from the chat.
- 🟩 **assistant** — configs, scripts, specs, porting the pilot. I prepare these in advance, without waiting for procurement.

**Critical path:** 🟦 real API keys (A1) — without them the foundation cannot start
(the pilot ran on a test DeepSeek key).

---

## Group A — Procurement and access (🟦 the team, steps run in parallel)

| # | Step | Acceptance |
|---|---|---|
| A1 | Production API keys: **Anthropic** (Opus — plan/review) + **z.ai** and/or **Moonshot** (GLM/Kimi — development) | key verified with a test request |
| A2 | A dedicated **Linux server, 24/7** (a cloud Ubuntu VM). Recommended: 8+ vCPU, 16+ GB RAM, 100+ GB disk, Docker | SSH access, `docker` works |
| A3 | **Jira**: a service account + API token for agents | token obtained → into Vault |
| A4 | **Telegram**: a bot token (BotFather) + the allowlist of users | token obtained → into Vault |
| A5 | Confirm the **Git platform** (GitHub / GitLab / Bitbucket) — the CI/CD format depends on it | named |

## Group B — Configs and code (🟩 assistant, prepared in advance)

| # | Step | Depends on |
|---|---|---|
| B1 | **CI/CD pipeline** for platform A5: build + unit/E2E tests + static analysis (SpotBugs/Checkstyle/SonarQube) + JaCoCo coverage | A5 |
| B2 | **Deploy scripts** for staging and prod (prod — gated by a recorded approval) | — |
| B3 | **Secrets layout** — Vault structure: which keys, who reads them, how they are supplied to containers | — |
| B4 | **Server `docker-compose`** — gateway + postgres + kafka + orchestrator, adapted for the production server (no code bind-mount) | — |
| B5 | **Production `litellm-config`** — Opus/GLM/Kimi by role instead of DeepSeek | A1 |
| B6 | **Intake bot** `intake/bot.js` per the `intake/bot.md` spec | A4 |

## Group C — Building the foundation on the server (after A + B)

| # | Step |
|---|---|
| C1 | Deploy the stack on the Linux server (`docker compose up -d`) |
| C2 | Branch protection on `main`; per-task worktree isolation in the orchestrator (the pilot did not have this) |
| C3 | Connect the Jira integration and the intake bot |
| C4 | Run CI/CD on a test PR — green |
| C5 | Drive 1-2 tasks through the pipeline on the production foundation |

---

## Phase 0 completion criterion

A task passes the pipeline on **production models**, **real CI/CD**, and **Git/PR** — up to `awaiting-approval`;
the prod deploy is by manual approval. After that → Phase 1 (`research_report` Stage 4: a pilot in PR mode).

## What is ported from the pilot (proven concepts)

Model routing by phase · the `triage→spec→plan→test_design→implement→gate` stages · the double gate ·
JaCoCo coverage · epistemic isolation of tests · a file-based task queue · agentic `implement`.
The hand-rolled orchestrator should be reassessed against a real harness (Hermes / Claude Code) in Phase 1,
as the hybrid plan envisaged. The pilot remains as a proven reference prototype.

## What is needed from the team now (so that B1-B6 become concrete)

1. **Git platform?** — GitHub / GitLab / Bitbucket / other.
2. **Production keys** — already available / coming / when? Which providers are you taking (Anthropic + z.ai? + Moonshot?).
3. **Linux server** — already available / to be provisioned? Cloud or own hardware?
