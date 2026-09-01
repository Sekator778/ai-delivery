# Mechanism provenance

Where each load-bearing mechanism came from and what we decided about it —
consolidated from the code comments and internal research notes that used to
be the only record. One row per mechanism: what it is, where it lives, where
it came from, verdict.

Verdicts:

- **adopted** — taken near-verbatim; only surface patches (tool frontmatter,
  paths). Upstream drift is checked by `ops/refresh-vendored-templates.sh`
  and should usually be ported.
- **adapted** — the idea is upstream's, the body is ours. Upstream diffs are
  informational only; never bulk-apply.
- **ours** — no upstream; usually born from a paid incident, cited per row.
- **retired** — tried and removed; kept here so it is not re-adopted blind.

Companion documents: [CALL-TREE.md](CALL-TREE.md) records *how* these
mechanisms hang together (and is drift-checked against the code);
[`.claude/agents/UPSTREAM`](../.claude/agents/UPSTREAM) and each vendored
template's `UPSTREAM` file record the exact file-level mappings.

## From BMAD-METHOD (v6.11.0, MIT)

| Mechanism | Where | Verdict |
|---|---|---|
| Three-lens reviewer: context-free blind hunt + mechanical path tracing + verification-gap analysis, orchestrator holds severity authority | reviewer stage in `stage_prompts.py`; personas `blind-hunter` / `edge-case-hunter` / `verification-gap` | **adapted** (#21; internal steal-list §2.1–3, §4.1–4) |
| Forced non-empty review quota — a "looks fine to me" pass is structurally impossible | `blind-hunter.md` (≥10 findings) | **adapted** (same steal-list batch) |
| Architecture-spine linter: lint the mechanical half of the Architect artifact so review judgment is spent on the semantic half | `architecture_lint.py` (from `lint_spine.py`) | **adapted** |
| Winston architect persona + pattern-detection-first discipline (patterns catalogued *before* design, ADR per deviation) | `architect.md`, `pattern-detector` stage, vendored `winston-architect/` template | **adapted** |
| BA Theater Check + ADR "Prevents" field — static prose gates against requirements theater | ba/architect prompts in `stage_prompts.py` | **adapted** |
| Brownfield discovery structure (prior decisions / relevant code / patterns to follow) | discovery stage prompt; vendored `document-project/` template | **adapted**; upstream later deprecated the skill — our fork is now the living copy |
| Edge-case hunting methodology (branch/boundary walk + deletion check) | `edge-cases` stage + reviewer lens; vendored `edge-case-hunter/` | **adapted** |

## From github/spec-kit

| Mechanism | Where | Verdict |
|---|---|---|
| Artifact contract: spec.md / plan.md / tasks.md as the pipeline's inter-stage currency (`/specify` → ba, `/plan` → architect, `/tasks` → tasks) | `SPEC_KIT_ARTIFACT` in `stage_prompts.py`; vendored `spec-kit/` templates | **adapted** |
| `/analyze` read-only cross-artifact consistency pass (detection passes, severity heuristic) | `analyze` stage prompt | **adapted** |

## From persona catalogues

| Mechanism | Where | Verdict |
|---|---|---|
| backend-developer, test-automator, security-auditor personas | `.claude/agents/` (from VoltAgent/awesome-claude-code-subagents) | **adopted** — only tool frontmatter patched |
| business-analyst, context-manager personas | `.claude/agents/` | **adapted** — initially misfiled as adopted; the first drift run (2026-08-15) showed both were rewrites, reclassified |
| team-lead / team-reviewer / team-debugger / team-implementer | `.claude/agents/` (from wshobson/agents) | **adopted** — used by the interactive `team-*` skills, not the pipeline |
| 22-persona catalogue "in case useful later" | — | **retired** 2026-08-15: 11 of 22 were dead weight; the roster now only holds personas dispatched by name (22 → 15) |

## From our own prior projects

| Mechanism | Where | Verdict |
|---|---|---|
| Auto-loop iteration semantics + idle watchdog | `auto_loop.py` (claude-tg-orchestrator `4aa02b0`, watchdog `fb4cdc8`) | **adopted** |
| Daemon lifecycle style: pidfiles, one-generation log rotation, detached start | `ops/atlas/aidstack.sh` (from an earlier private stack script of the operator's) | **adopted** |
| Folder-as-state-machine task queue (`inbox/ → active/ → … → done/`) | `task_dispatcher.py`, `tasks/` | **ours** (Phase 5 design) |
| Semantic recall over a vector DB (mem0-era `meta_agent_mem` in Qdrant + TEI bge-m3) | `memory_inject.py`, `scripts/qdrant-memory.py` | **ours**; 2026-08-21 verdict (backlog/T13): not justified at this scale — 600 MB and two always-on services for 3.2 MB of vectors, 787 of 809 points frozen prose from hooks retired in `a364eb6`. Flat store recommended, Qdrant dropped, semantics kept; awaiting owner GO — see ROADMAP "Phase Memory-Footprint" |

## Incident-born (ours)

Each of these exists because a specific failure was paid for once.

| Mechanism | Where | Incident |
|---|---|---|
| Process-group ownership + orphan reaping; `down` sweeps ppid-1 claude children | `proc_reaper.py` | #18 — an orphaned claude burned the subscription 3h11m unnoticed (2026-08-14) |
| Limit-storm detection on the live output stream; park instead of timeout | `limit_stall.py` | #11 — `capture_output=True` made the runner blind to the storm it was inside |
| Ephemeral worktree isolation for developer+ stages | `git_pr.py` / runner | a self-targeted run's branch switch made the live deployment's files vanish mid-run |
| Pipeline-owned `CLAUDE_CONFIG_DIR` + macOS keychain credential seeding | `pipeline_config.py` | first version shipped without seeding — every stage died "Not logged in" |
| Minimal child env — no operator-shell inheritance | `child_env.py` | #13 |
| Stage cwd = target project; personas via `--agents` | `agent_roster.py` | stages read *this framework's* CLAUDE.md while developing other repos; internal research rule: the target's CLAUDE.md/AGENTS.md must reach the stage |
| Branch/PR lock restored on resume | runner state | `8f7619e` — a fully green task died rc=5 after $14.56 because the resumed run lost its lock |
| `allowed_warning` is not limit exhaustion | `limit_stall.py` | `e2007b8` — healthy stages were parked on a status-line warning |
| Adaptive complexity triage — drop redundant *upstream* reasoning stages only; review/test/security never drop | `triage.py` | an S-tier task burned ~$18 on upstream reasoning + a 3-iteration loop |
| Tier-L build stages start on anthropic, no DeepSeek first try | `backend_routing.py` | DeepSeek timed out a real L developer stage mid-build (2026-06-03) |
| Stage timeout returns a dedicated rc, skips the auto-fallback retry | `stage_runner_agent.py` | a second full timeout window was ~half of a $17.31 incident |
| Cost-cap park to `awaiting-input` instead of silent kill | `budget_gate.py` | runaway meta-run spend |
| Smoke run on a sandbox is part of the contract for runner changes | ops rule (see sandboxes in `~/projects/ai-delivery-sandbox*`) | three 2026-08-15 bugs (branch/PR lock, false park, "Not logged in") were all environment-interaction bugs unit tests could not catch; the run costs $2–3, the incident cost $14.56 |

## Maintaining this file

Add a row when a mechanism is adopted, adapted, or born from an incident —
in the same PR. The sources above are also cited at the point of use in code
comments; this table is the index, not a replacement. File-level vendoring
detail stays in the `UPSTREAM` files and is drift-checked by
`ops/refresh-vendored-templates.sh`; call-graph facts stay in
[CALL-TREE.md](CALL-TREE.md) and are drift-checked by
`ops/check-arch-map.py`.
