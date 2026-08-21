# .claude/agents — pipeline agent definitions

Sub-agent catalog dispatched by the pipeline: since 2026-08-15 the stage
runner passes these personas to each stage's `claude -p` subprocess via
`--agents` (see `dispatcher/agent_roster.py`) — stages run from the TARGET
repo, so nothing is read from a cwd `.claude/agents/` anymore. The same
catalog also serves the interactive `team-*` slash commands and the
meta-agent (`bot.py` → meta-Claude Q&A).

## Source and sync

Two upstream catalogs, plus BMAD for the review lenses:

1. **[VoltAgent/awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents)**
   — the pipeline stage backbone.
2. **[wshobson/agents](https://github.com/wshobson/agents)**
   — the parallel-team agents behind the `team-*` slash commands.
3. **[BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD)** v6.11.0 (MIT)
   — the three review lenses, adapted rather than vendored.

**[`UPSTREAM`](UPSTREAM) is the authority**: it records where every file came
from, which sync class it belongs to (VENDORED / ADAPTED / OURS), and what was
patched locally. Check for drift with `ops/refresh-vendored-templates.sh` —
read-only, prints a report path, never writes into the tree.

**The roster is exactly the working set.** A file lives here only if something
dispatches it by name, or it is documentation. That is enforced by
`tests/test_agent_roster.py`, in both directions: an orphan persona fails, and
so does a dispatched name with no file. Six personas were deleted on 2026-08-15
(`architect-review`, `backend-architect`, `business-analyst-kpi`,
`microservices-architect`, `tdd-orchestrator`, `python-pro`) — imported from
upstream on the theory that they might be useful, never wired to a consumer.
Do not re-add one until the thing that dispatches it exists.

## Pipeline stage mapping

The 6-stage pipeline (BA → Architect → Developer → Tester → Security → Reviewer)
maps onto these agents:

| Stage     | Agent              | Model  | Tools                                          |
|-----------|--------------------|--------|------------------------------------------------|
| BA        | `business-analyst` | sonnet | `Read, Write, Edit, Glob, Grep, WebFetch, WebSearch` |
| Architect | `architect`        | opus   | `Read, Write, Edit, Bash, Glob, Grep`          |
| Developer | `backend-developer` | sonnet | `Read, Write, Edit, Bash, Glob, Grep` |
| Tester    | `test-automator`   | sonnet | `Read, Write, Edit, Bash, Glob, Grep`          |
| Security  | `security-auditor` | opus   | `Read, Grep, Glob` (read-only)                 |
| Reviewer  | three lenses + orchestrator triage (see below) | inherit | `Read, Grep, Glob` (read-only) |

## Reviewer lenses (from BMAD-METHOD v6.11.0, MIT)

The Reviewer stage is not one persona: `STAGE_PROMPTS["reviewer"]` dispatches
three lenses in parallel, each with an INDEPENDENT context (the diff + its own
brief, never another lens's findings), then the stage orchestrator triages them
itself as the single severity authority (#21, `research/bmad-steal-list.md`
§2 items 1-3).

| Agent               | Model   | Tools              | Lens                                                                 |
|---------------------|---------|--------------------|----------------------------------------------------------------------|
| `blind-hunter`      | inherit | `Read, Grep, Glob` | Context-free adversarial pass, forced quota of ≥10 findings           |
| `edge-case-hunter`  | inherit | `Read, Grep, Glob` | Pure path tracer over the diff + deletion check on removed code       |
| `verification-gap`  | inherit | `Read, Grep, Glob` | Would a real test catch the regression? Demonstration-then-check      |
| `code-reviewer`     | opus    | `Read, Grep, Glob` | Stage persona for `--stage reviewer` / the `edge-cases` stage         |

`model: inherit` implements BMAD's "all review subagents run at the same model
capability as the current session" without defeating the pipeline's tier-based
backend routing. Lenses propose a severity; the orchestrator disregards it
whenever the code says otherwise.

## Complementary catalog (from wshobson)

| Agent                  | Source plugin             | Model   | Tools (after override)                | Role                                    |
|------------------------|---------------------------|---------|----------------------------------------|-----------------------------------------|
| `team-lead`            | agent-teams               | opus    | `Read, Glob, Grep, Bash, Agent, Team*, Task*, SendMessage` | Decomposes work, owns file boundaries, synthesizes results |
| `team-reviewer`        | agent-teams               | opus    | `Read, Glob, Grep, Bash, Task*, SendMessage` | Dimension-specific reviewer for parallel fan-out (security / perf / architecture / testing / a11y) |
| `team-debugger`        | agent-teams               | opus    | `Read, Glob, Grep, Bash, Task*, SendMessage` | Hypothesis-driven investigator — one hypothesis per agent, structured evidence |
| `team-implementer`     | agent-teams               | opus    | `Read, Write, Edit, Glob, Grep, Bash, Task*, SendMessage` | Parallel feature builder with strict file ownership |
| `context-manager`      | agent-orchestration       | inherit | (upstream all)                         | Dynamic context engineering + cross-session memory orchestration |

These augment the VoltAgent base — the pipeline stage backbone (BA / architect /
developer / tester / security / reviewer) is the table above; the wshobson set
is what `team-spawn` and `team-review` slash commands draw from when fanning
out parallel work (see `.claude/commands/`).

## ai-delivery overrides from upstream

- **`code-reviewer.md`** (VoltAgent) — upstream ships
  `Read, Write, Edit, Bash, Glob, Grep`. We strip Write/Edit/Bash so the
  reviewer is *physically* incapable of writing code, as called out in the
  May 2026 improvement plan (the reviewer is physically unable to write code —
  enforced by tool restrictions at the tooling level, not the prompt). If a
  finding requires a fix, the reviewer surfaces it; it does not patch.
  The BODY was also replaced (#21): upstream's generic checklist ("code coverage
  > 80% confirmed", "cyclomatic complexity < 10 maintained") carried no
  methodology and no evidence discipline — the weakest link in the pipeline per
  `research/bmad-steal-list.md` §1. It now runs the same read-before-you-claim
  discipline as the three lenses.
- **`blind-hunter.md` / `edge-case-hunter.md` / `verification-gap.md`**
  (BMAD-METHOD v6.11.0, MIT) — adapted, not vendored verbatim: native agent
  definitions instead of `{skill-root}` instruction files resolved by the
  `_bmad/` runtime, read-only tools enforced in frontmatter, every interactive
  halt stripped (they run unattended under `claude -p`), one shared markdown
  findings block instead of three output dialects, and severity emitted as an
  explicitly labelled proposal (upstream forbids severity outright; we need a
  starting signal for the orchestrator's triage buckets).
- **`architect.md`** (VoltAgent) — a trimmed rename of upstream's
  `microservices-architect.md`, matching our pipeline stage name. 55 lines
  against upstream's 238: the trimming IS the adaptation, so its upstream diff
  is informational only (ADAPTED class — never bulk-apply it).
- **`backend-developer.md`** (VoltAgent) — upstream's opening claimed "deep
  expertise in Node.js 18+, Python 3.11+, and Go 1.21+". A persona dispatched
  for arbitrary target repositories cannot carry a language list; it is now
  language-agnostic and establishes the stack from the target's own
  `CLAUDE.md` / `AGENTS.md` first. Upstream's "Query context manager" step went
  with it — `context-manager` is the Discovery stage's own persona here, not a
  service a stage can query.

**Second cleanup pass (2026-08-15):** all three VoltAgent personas
(`backend-developer.md`, `test-automator.md`, `security-auditor.md`) had the
rest of upstream's dead inter-agent machinery removed — the "Query context
manager" step, the `requesting_agent` JSON protocol blocks, the canned
delivery notifications with fabricated statistics, and the "Integration with
other agents" lists naming agents this roster does not have. Each carries a
provenance comment; `tests/test_prompt_placeholders.py` pins the removals so
a re-vendor cannot silently restore them.

## How to add a new agent

0. First: name the consumer. A persona with nothing dispatching it will fail
   `tests/test_agent_roster.py`, and that is deliberate — the previous roster
   reached 22 files of which 11 were dead.
1. Fetch verbatim from upstream or write from scratch.
2. Enforce the role-class tool restriction (VoltAgent CLAUDE.md convention):
   - **Read-only** (reviewers, auditors): `Read, Grep, Glob`
   - **Research** (analysts): `Read, Grep, Glob, WebFetch, WebSearch`
   - **Code-writing** (devs, testers): `Read, Write, Edit, Bash, Glob, Grep`
   - **Documentation**: `Read, Write, Edit, Glob, Grep, WebFetch, WebSearch`
3. If you patch upstream, leave an HTML comment near the frontmatter saying
   what changed and why, so the next sync notices the diff.
4. Record it in [`UPSTREAM`](UPSTREAM) with its sync class, and — if VENDORED —
   add the mapping line to `ops/refresh-vendored-templates.sh`. Both are
   checked by `tests/test_agent_roster.py`; a persona nobody can sync is how
   the last catalogue drifted for three months without anyone noticing.

## How these are actually dispatched

`dispatcher/stage_runner_agent.py` spawns `claude -p` per stage, and each stage
prompt in `dispatcher/stage_prompts.py` dispatches its persona by name
(`subagent_type = "<name>"`). Eleven personas are reached this way: the eight in
the stage table above plus the three reviewer lenses.

Two consequences worth knowing before changing anything here:

- **`STAGE_AGENT_MAP` does not select the persona.** Despite the name, its only
  consumer is the `--stage` CLI flag's `choices=` list. The persona that runs is
  the literal string inside the prompt text.
- **The stage process's working directory is this repo, not the target.** No
  `cwd` is passed to the spawn, so the child inherits the daemon's directory.
  That is what makes `.claude/agents/` resolvable at all (nothing is installed
  under `~/.claude/agents/`) — but it also means Claude Code auto-loads
  *ai-delivery's* `CLAUDE.md` into every stage, never the target's. The stage
  prompts therefore read `{target_repo}/CLAUDE.md` and `{target_repo}/AGENTS.md`
  explicitly (2026-08-15). Moving `cwd` into the target worktree would be the
  cleaner fix and requires installing these personas at user level first.

**Language policy:** no persona names a default language. The framework builds
applications in any language, and the language comes from the target repo's own
instructions to LLM agents (`CLAUDE.md` / `AGENTS.md`), then from the
Pattern-Detection report, then from the repo's manifests — in that order of
authority. `python-pro` is not dispatched by any stage; the developer prompt
cites it as reading material for Python targets.
