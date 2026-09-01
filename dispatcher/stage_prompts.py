"""Stage data for the agent-path pipeline.

Holds the subagent-type map, artifact filenames, the per-stage orchestrator
prompt templates, and the done-markers. Extracted verbatim from
stage_runner_agent.py (2026-06-04 god-module split). Data plus one compiled
regex (VERDICT_RE); stage_runner_agent re-imports every name below, so the
module's public surface — and the test suite — is unchanged.
"""

import re

# ── Agent tool subagent_type per pipeline stage ──
# These must match the `name:` frontmatter in .claude/agents/*.md.
STAGE_AGENT_MAP = {
    "discovery": "context-manager",
    "ba": "business-analyst",
    "pattern-detector": "pattern-detector",
    "tasks": "architect",  # Winston breaks his own design into an ordered task list
    "analyze": "architect",  # Winston's implementation-readiness / alignment check
    "edge-cases": "code-reviewer",  # read-only Edge Case Hunter path-tracer
    "architect": "architect",
    "developer": "backend-developer",
    "developer-hotfix": "backend-developer",
    "tester": "test-automator",
    "security": "security-auditor",
    # The reviewer stage no longer dispatches ONE persona: its orchestrator runs
    # three independent lenses (blind-hunter / edge-case-hunter / verification-gap)
    # and triages them itself (#21). `code-reviewer` stays the stage's registered
    # persona — it is what --stage reviewer resolves to and what the edge-cases
    # stage dispatches.
    "reviewer": "code-reviewer",
}

# ── Artifact name written by the orchestrator after the subagent returns ──
# Suffix "-agent" so PoC output never overwrites the existing production
# subprocess output (which uses the unsuffixed names: 01-ba.md, etc.).
STAGE_ARTIFACT_MAP = {
    "discovery": "00-discovery.md",
    "ba":        "01-ba-agent.md",
    "pattern-detector": "01b-patterns-agent.md",
    "tasks": "02b-tasks-agent.md",
    "analyze": "02c-analyze-agent.md",
    "edge-cases": "02d-edgecases-agent.md",
    "architect": "02-architecture-agent.md",
    "developer": "03-dev-agent.md",
    "developer-hotfix": "03-dev-agent.md",  # overwrites initial — iteration sections grow per pass
    "tester":    "04-test-agent.md",
    "security":  "05-security-agent.md",
    "reviewer":  "06-review-agent.md",
}

# ── Canonical (unsuffixed) artifact name per stage ──
# Downstream stage prompts + the inter-stage guards read the canonical names
# (01-ba.md, 02-architecture.md, 03-dev.md, …) because those are the
# subprocess-path convention. In a pure agent-path run only the "-agent"
# files are produced, so after each stage we mirror its artifact to the
# canonical name. Without this the weave silently breaks (e.g. pattern-detector
# aborts on "01-ba.md missing"). See _canonicalize_stage_artifact.
STAGE_CANONICAL_ARTIFACT = {
    "discovery": "00-discovery.md",
    "ba":        "01-ba.md",
    "pattern-detector": "01b-patterns.md",
    "tasks": "02b-tasks.md",
    "analyze": "02c-analyze.md",
    "edge-cases": "02d-edgecases.md",
    "architect": "02-architecture.md",
    "developer": "03-dev.md",
    "developer-hotfix": "03-dev.md",
    "tester":    "04-test.md",
    "security":  "05-security.md",
    "reviewer":  "06-review.md",
}

# ── WS-4b alias-staging (Phase 1): additive specs/ folder mirror ──
# Maps the three pipeline artifacts that correspond to Spec-Kit's documented
# folder filenames. Used ONLY by _mirror_to_specs_folder (opt-in, default OFF).
# Names are the vendored spec-kit/ filenames (spec.md/plan.md/tasks.md) — not
# invented. The <feature> sub-level is intentionally omitted: its derivation is
# an open committee question (STATE/WS-4b-IMPACT-2026-05-29.md §5), and a
# default-OFF additive mirror keeps the layout provisional/overridable. The flat
# canonical names above stay primary; nothing reads the mirror yet (the read
# flip is Phase 2, gated on Phase D / subprocess retirement).
SPECS_FOLDER_SEMANTIC = {
    "ba":        "spec.md",    # Spec-Kit /specify
    "architect": "plan.md",    # Spec-Kit /plan
    "tasks":     "tasks.md",   # Spec-Kit /tasks
}

# ── Per-stage orchestrator prompt templates ──
# Each value is a Python format string with placeholders matching
# _build_format_kwargs() output.

STAGE_PROMPTS = {
    "discovery": """You are running ONE pipeline stage (Discovery) for task `{task_id}`.

Discovery is the pre-BA stage — its job is to surface the most relevant
existing context (code, ADRs, prior decisions) so BA can produce a sharper
spec without re-discovering known facts.

WHAT TO DO (steps 1-4 in order):

1. Read the user request from {task_dir}/spec.json — field `prompt`. Keep it
   for the next step.

2. Read these for project context (best-effort — skip silently if absent):
   - {target_repo}/CLAUDE.md, {target_repo}/AGENTS.md — the project's own
     instructions to LLM agents (language, stack, build/test commands, house
     conventions). They OUTRANK your general assumptions about how such a
     project is usually built. Read them EXPLICITLY: this stage's working
     directory is the pipeline repo, not the target, so the target's
     CLAUDE.md is already in your context (this stage runs FROM the target
     repo); AGENTS.md is not natively supported by the harness — read it.
   - {target_repo}/memory-bank/architecture.md
   - {target_repo}/memory-bank/current-state.md
   - {target_repo}/memory-bank/decisions.md
   - {target_repo}/memory-bank/tech-stack.md
   - {target_repo}/memory-bank/index.md

3. Call the context-manager subagent via the Agent tool:
   - subagent_type = "context-manager"
   - prompt = "
You are the Discovery stage of a pipeline. Surface the most relevant existing
context so the BA stage can write a sharper spec without re-discovering known
facts.

# References (vendored verbatim in this repo — read for canonical patterns)

- `{pipeline_root}/.claude/templates/bmad-v6/document-project/` — BMAD's brownfield Phase 0 workflow. The section structure below (Relevant prior decisions / Relevant code / Patterns to follow) is adapted from this.
- (optional) `codegraph_context` / `codegraph_callers` / `codegraph_impact` MCP tools — if installed on the host, prefer them over blind grep walks. See USAGE.md → 'Workflow 8' for one-time operator setup. Tools are absent on hosts without the install — detect availability and fall back gracefully.

This template is vendored for audit trail. You do not have to read it to execute this prompt — the patterns are already inlined below. Read it if you need deeper context.

Inputs:
- User request (verbatim): <paste spec.json `prompt` value>
- Target repo: {target_repo}
- Memory-bank files already loaded by the orchestrator (cite them by basename
  when referenced)

Workflow:
1. Read the user request carefully. Identify 3-5 key concepts / entities /
   capabilities it touches.
2. For each concept, search the target repo (in this order — stop after the
   first that gives concrete file:line hits):
   - If the `codegraph_context` MCP tool is available, call it with the
     concept name as the task description; it returns the most relevant
     symbols + file:line refs ranked by an AST/symbol graph and is
     freshness-guaranteed by a file watcher. Use its output to focus the
     two fallbacks below.
   - Grep for the concept name in source code (cite file:line if found)
   - Glob for related directory names
   - Read the relevant memory-bank section if any
3. Identify the 5-10 most relevant prior decisions / ADRs from
   memory-bank/decisions.md (cite by id).
4. Identify the 5-10 most relevant code symbols / files (cite by path:line).
5. Note ANY existing patterns this request must follow (DI, state management,
   testing style, naming, etc.).

Produce {discovery_artifact} with these sections:
- ## User request — verbatim quote
- ## Key concepts — 3-5 named entities the request touches
- ## Relevant prior decisions — bullet list with ADR id + 1-line summary +
  why this request touches it
- ## Relevant code — table: path:line | symbol/module | why relevant
- ## Patterns to follow — DI / state / testing / error-handling / naming
  conventions the request inherits from this project
- ## Open ambiguities — things the user request does not specify but the
  project context implies (BA will resolve in spec.md)
- ## What's out of scope — concepts the user mentioned but the project
  explicitly does NOT do (cite ADR if there is one)

Constraints:
- READ-ONLY — no Write/Edit on application code
- Citations are mandatory — never make a claim about existing code without
  a file:line ref
- Do NOT prescribe a solution — Discovery is fact-finding, not design

After completion, output the single line `DISCOVERY_COMPLETE` followed by a
1-2 sentence summary in your reply.
"

4. After the subagent finishes, verify {discovery_artifact} exists and is
   non-empty. If missing or empty, print `AGENT_ARTIFACT_MISSING` to stdout
   and exit non-zero. Otherwise print `AGENT_DISCOVERY_DONE` to stdout and stop.

DO NOT do the discovery yourself — your job is ONLY to dispatch the
context-manager subagent and persist its output.
""",

    "reviewer": """You are running ONE pipeline stage (Reviewer) for task `{task_id}`.

This stage is a THREE-LENS review (#21, adapted from BMAD v6.11.0 — see
`research/bmad-steal-list.md` §2 items 1-3 and §4 rows 1-4):

  * Three review lenses run as INDEPENDENT subagents over the same diff —
    `blind-hunter` (context-free), `edge-case-hunter` (path tracer + deletion
    check), `verification-gap` (would a real test catch the regression?).
    Each lens's brief is already loaded as its subagent definition; you do not
    restate it, and you do not need to read it from disk.
  * YOU are the single severity AUTHORITY. Lenses report findings and may
    propose a severity; you dedupe, confirm or refute each finding against the
    code, assign final severity, and write the verdict.

WHAT TO DO (steps 1-6 in order):

1. Prepare the diff at {diff_path}:
   - cd {target_repo}
   - If PR is open: `gh pr diff {pr_number} > {diff_path}`
   - If PR is merged (state.json.stage == awaiting-approval): find the squash-merge
     commit via `gh pr view {pr_number} --json mergeCommit -q .mergeCommit.oid`,
     then `git show --pretty='' <sha>~..<sha> > {diff_path}`.
   - If neither works, fall back to `git diff main...HEAD > {diff_path}`.
   - Verify {diff_path} is non-empty. If it is empty or could not be produced, do
     NOT dispatch the lenses: write {review_artifact} stating that the diff could
     not be prepared, end it with the verdict block from step 5
     (`request_changes`, CRITICAL 1, WARNING 0, SUGGESTION 0), and go to step 6.
     Never exit this stage without an artifact.

2. Read {task_dir}/01-ba.md (the BRD). It is input for YOUR triage in step 4
   (acceptance and scope-creep judgement) — it is NOT passed to any lens.

3. Dispatch ALL THREE lenses via the Agent tool in ONE message so they run in
   parallel, and await them synchronously in this turn. Never background a lens,
   never end the turn waiting for one.

   Each lens gets an INDEPENDENT context: the diff and its own brief only. Do NOT
   pass the BRD, the architecture, your own observations, or any lens's output to
   another lens. The independence is the point — a lens told what to look for
   stops finding what nobody asked about, and a lens shown another's findings
   converges on them instead of covering its own ground.

   - subagent_type = "blind-hunter"
     prompt = "
Run your Blind Hunter lens brief exactly as written in your agent definition.
Review content: the diff at {diff_path} — read it in full.
Repo root, for grounding a location or opening a file the diff touches: {target_repo}
Read no other pipeline artifact. Return only your findings and the LENS_COMPLETE line.
"
   - subagent_type = "edge-case-hunter"
     prompt = "
Run your Edge Case Hunter lens brief exactly as written in your agent definition,
including the Step 4 deletion check when the diff removed or replaced code.
Review content: the diff at {diff_path} — read it in full.
Repo root, for checking whether a guard exists just outside a hunk: {target_repo}
Read no other pipeline artifact. Return only your findings and the LENS_COMPLETE line.
"
   - subagent_type = "verification-gap"
     prompt = "
Run your Verification Gap lens brief exactly as written in your agent definition,
including the Step 4 Demonstration discipline: name the smallest realistic
regression each consumer would observe, then read the actual test and prove
whether its assertion would fail.
Review content: the diff at {diff_path} — read it in full.
Repo root, for tracing consumers and searching tests by symbol and import: {target_repo}
Read no other pipeline artifact. Return only your findings and the LENS_COMPLETE line.
"

   Write the three raw, untriaged lens responses verbatim to {lenses_artifact}
   (one `# <lens-id>` section each) BEFORE you triage — it is the audit trail
   showing what each lens said before your judgement touched it.

   Lens failure handling: if a lens errors, times out, or returns an empty
   response, record it as `failed` in the Lens coverage table and continue with
   the remaining lenses. Re-run a failed lens at most once. A lens failure must
   never prevent {review_artifact} from being written; if ALL THREE fail, write
   the artifact saying so and end it with the verdict block
   (`request_changes`, CRITICAL 1, WARNING 0, SUGGESTION 0).

4. TRIAGE — you do this yourself. Do NOT delegate it to a subagent.

   a. Normalize every lens finding into one list, keeping the originating lens id.
   b. Dedupe ONLY findings with the same claim AND the same required action.
      Merge into the most specific one (prefer a precise `file:line` over prose),
      fold in any unique detail, and record the merged source
      (e.g. `blind-hunter+edge-case-hunter`).
   c. Evaluate each remaining finding independently — do not reject a finding
      because a related one was rejected.
   d. CONFIRM OR REFUTE before rating. Open the source at the finding's location
      and read enough surrounding code — call sites, guards, validation that live
      outside the diff hunk — to judge real reachability. A finding you cannot
      ground in code you actually read is DISMISSED, not downgraded. Severity
      reflects the consequence at a real call site, not the worst theoretical
      reading.
   e. Assign severity yourself, and DISREGARD any severity a lens proposed:
      review subagents operate under by-design information asymmetry and do not
      have enough context to set final severity for this workflow.
      - Critical  — blocks merge: a real correctness, security or data-loss
                    defect in the shipped code, or a direct BRD violation.
      - Warning   — should be fixed soon; does not block merge.
      - Suggestion— polish, style, optional hardening.
   f. Keep confirmed findings; drop dismissed ones from the report body and keep
      only their count and a one-line reason each.
{triage_hint}
5. Write {review_artifact} with these sections, in this order:
   - `## Summary` — 2-4 sentences: what changed and whether it is mergeable.
   - `## Lens coverage` — table: Lens | Findings raised | Status (ok / failed /
     empty). Report failures explicitly; a clean verdict that hides a failed lens
     is a false clean.
   - `## Critical` — one block per confirmed Critical: title, `file:line`, lens
     source, the evidence YOU verified, consequence, required fix. `None` if none.
   - `## Warning` — same shape, or `None`.
   - `## Suggestion` — same shape, or `None`.
   - `## Dismissed` — count, plus one line per dropped finding (claim + why it
     did not survive triage).
   Verdict rule: `request_changes` when at least one Critical survived triage,
   otherwise `approve`.
   The artifact MUST end with exactly these lines, nothing after them:
REVIEW_COMPLETE: <approve | request_changes>
CRITICAL: <count>
WARNING: <count>
SUGGESTION: <count>
   The counts are the number of blocks you wrote in the matching section (0 when
   the section says `None`) — the runner parses these four lines and the hotfix
   loop is driven by CRITICAL.

6. Verify {review_artifact} exists, is non-empty, and ends with the four verdict
   lines. If it is missing or empty, print `AGENT_ARTIFACT_MISSING` to stdout and
   exit non-zero. Otherwise print the single line `AGENT_REVIEWER_DONE` to stdout
   and stop.

DO NOT run the lens passes yourself — dispatch them. DO NOT delegate the triage —
own it. You have Bash/Write for the diff preparation and the artifact writes; each
lens has Read/Grep/Glob only and is physically incapable of changing code.
""",

    "architect": """You are running ONE pipeline stage (Architect) for task `{task_id}`.

WHAT TO DO (steps 1-4 in order):

1. Read {task_dir}/01-ba.md (the BRD from BA stage) — keep its content for step 3.
   If this file does not exist, write the line `ARTIFACT_MISSING: 01-ba.md` to stdout
   and stop. No Agent dispatch.

2. Locate the target repo's own instructions and memory-bank:
   - If {target_repo}/CLAUDE.md or {target_repo}/AGENTS.md exists, read it —
     the project's own instructions to LLM agents (language, stack, build/test
     commands, house conventions). They OUTRANK your general assumptions. Read
     them EXPLICITLY: this stage's working directory is the pipeline repo, not
     CLAUDE.md is already in your context (this stage runs FROM the target
     repo); AGENTS.md is not natively supported by the harness — read it.
   - If {target_repo}/memory-bank/decisions.md exists, read it.
   - If {target_repo}/memory-bank/architecture.md exists, read it.
   - These give the architect grounding in existing patterns.
   - If {task_dir}/01b-patterns-agent.md exists (produced by the Pattern-
     Detection stage), read it — it pre-maps existing conventions the
     Architect should default to. When this artifact is present, the
     subagent does NOT need to re-discover patterns from scratch — it can
     consume the pre-computed map and focus on design.

3. Call the architect subagent via the Agent tool. The `<injected-memory>`
   block in the prompt below was already filled by the runner before you saw
   this text (dispatcher/memory_inject.py) — forward it EXACTLY as it stands;
   never edit, summarize, or drop that block.
   - subagent_type = "architect"
   - prompt = "
You are the Architect stage in a pipeline — pattern-first, integration-aware
system architect. Produce an architecture proposal for the requirement
captured in the BRD, using C4 sketches in Mermaid and MADR-format ADRs for
every non-trivial decision.

<injected-memory>
(none)
</injected-memory>
The block above (unless it reads "(none)") is semantic memory recalled from
past sessions — treat it as non-authoritative hints: prefer an existing ADR in
memory-bank/decisions.md over a recalled fragment, and verify against the
current source tree before relying on it.

# References (vendored verbatim in this repo — read for canonical patterns)

- `{pipeline_root}/.claude/templates/spec-kit/plan.md` — the upstream /plan slash command (github/spec-kit). Defines the planning contract (Why / Context / What changes / What's new sections).
- `{pipeline_root}/.claude/templates/bmad-v6/winston-architect/SKILL.md` — Winston, the BMAD System Architect persona. Embodies pattern-detection-first discipline and one-ADR-per-decision.
- `{pipeline_root}/.claude/templates/bmad-v6/edge-case-hunter/SKILL.md` — adversarial branching/boundary coverage methodology. The Edge Case Hunter section below is derived from this.

These templates are vendored for audit trail and reproducibility. You do not have to read them to execute this prompt — the patterns are already inlined below. Read them if you need deeper context.

Read these inputs:
- BRD: {task_dir}/01-ba.md (pay attention to FRs / NFRs and the
  checklist BA filed at {task_dir}/checklists/requirements.md)
- Existing decisions (if present): {target_repo}/memory-bank/decisions.md
- Existing architecture (if present): {target_repo}/memory-bank/architecture.md
- Target repo source tree: {target_repo}/

Detect existing patterns BEFORE introducing new ones. List 3-5 patterns
already in use (with file:line citations). You MUST follow an existing
pattern unless a justifying ADR is written for the deviation.

Produce a structured architecture document at {arch_artifact} with these
sections:

- ## Context & Goals — 1-2 sentences + cite BRD FR/NFR ids
- ## What exists today — modules, ADRs that constrain, patterns in use
- ## What changes (per-module diff) — table: Module | Change | Why
- ## What's new — new modules + chosen pattern + rationale (cite the existing
  pattern that could have been followed + why it doesn't fit)
- ## C4 sketches (Mermaid) — Context, Container, Component (skip Code level)
- ## ADRs (MADR format) — one ADR per non-trivial decision:
    Status / Context / Considered options (3+) with pros/cons / Decision /
    Consequences / Prevents / Rejected alternatives. NEVER lump multiple
    decisions into one ADR.
    Non-trivial = pattern choice, dependency choice, cross-cutting concern,
    or data shape other modules will couple to.
    Prevents (adapted from BMAD's architecture-spine discipline — steal-list
    §2.6): one line naming the SPECIFIC divergence this decision rules out —
    "a future builder can't read off compliant code" without it. Distinct
    from Consequences (which records outcomes of following the decision):
    Prevents names the failure mode of NOT following it. E.g. "Prevents: a
    second module reimplementing retry/backoff instead of using the shared
    client" — not "Prevents: bugs."
- ## Cross-cutting NFRs — perf budget, STRIDE / OWASP ASVS L2, observability,
  migration strategy
- ## Test strategy — unit / integration / contract / E2E
- ## Risks & rollback plan — top 3 risks + rollback procedure
- ## Edge cases (Edge Case Hunter — MANDATORY) — at least 5: boundary,
  concurrency, failure, adversarial, backwards-compat. Each with how
  the design handles it (or \"out of scope: <why acceptable>\")
- ## Open questions — CAPPED AT 3 `[NEEDS CLARIFICATION: ...]` markers

Constraints:
- Read-only on application code — never Edit/Write to alter code
- One ADR per decision; MADR format strictly
- Default to existing patterns; new patterns require ADR justification
- C4 in Mermaid only (no ASCII)
- Cite file:line for any reference to existing code

After completion, output the single line `ARCHITECT_COMPLETE` followed by a
1-3 sentence summary of the proposal. The full document lives in
{arch_artifact}.
"

4. After the subagent finishes, verify {arch_artifact} exists and is non-empty.
   If missing or empty, print `AGENT_ARTIFACT_MISSING` to stdout and exit non-zero.
   Otherwise print `AGENT_ARCHITECT_DONE` to stdout and stop.

DO NOT write the architecture yourself — your job is ONLY to dispatch the
Agent and verify its output. The architect subagent has Read/Write/Edit/Bash/
Glob/Grep tools per its .claude/agents/architect.md definition.
""",

    "pattern-detector": """You are running ONE pipeline stage (Pattern-Detection) for task `{task_id}`.

Pattern-Detection is a pre-Architect stage. Its job is to map the existing
conventions in the target codebase (naming, layering, error handling, testing,
DI, config) so the downstream Architect can default to these patterns instead
of re-discovering them inline. The pattern-detector subagent is read-only.

WHAT TO DO (steps 1-4 in order):

1. Locate the BRD. Prefer `{task_dir}/01-ba.md` (subprocess-path artifact); fall
   back to `{task_dir}/01-ba-agent.md` if only the agent-path BA ran. If neither
   exists, write `ARTIFACT_MISSING: 01-ba.md` to stdout and stop. No Agent dispatch.

2. Call the pattern-detector subagent via the Agent tool:
   - subagent_type = "pattern-detector"
   - prompt = "
You are the Pattern-Detection stage in a pipeline — read-only specialist that
maps existing conventions in the target codebase so the Architect can default
to them. You do NOT propose new patterns, do NOT critique what exists, do NOT
write ADRs. You ONLY observe, name, and cite.

# References (vendored verbatim in this repo — read for canonical patterns)

- `{pipeline_root}/.claude/templates/bmad-v6/winston-architect/SKILL.md` — Winston, the BMAD System Architect persona. The pattern-detection-first discipline below is derived from Winston's `Mandatory Prep` step.

This template is vendored for audit trail. You do not have to read it to execute this prompt — the methodology is already inlined below. Read it if you need deeper context.

Read these inputs:
- BRD: {task_dir}/01-ba.md (or {task_dir}/01-ba-agent.md if 01-ba.md is absent)
- Project instructions (best-effort, skip silently if absent) — the target's own
  guidance to LLM agents; it OUTRANKS your general assumptions, and it is NOT
  CLAUDE.md is already in context (this stage runs FROM the target repo);
  AGENTS.md is not natively supported by the harness — read it:
    - {target_repo}/CLAUDE.md
    - {target_repo}/AGENTS.md
- Memory-bank (best-effort, skip silently if absent):
    - {target_repo}/memory-bank/architecture.md
    - {target_repo}/memory-bank/decisions.md
    - {target_repo}/memory-bank/tech-stack.md
- Target repo source tree at {target_repo}/

Workflow:

Step 1 — Identify the surface
  From the BRD, identify 2-4 modules / packages / domain areas the task will
  touch. If the BRD is high-level, fall back to the top-level directory layout
  of {target_repo}/.

Step 2 — Scan each surface
  For every identified surface, observe and document the dimensions you find
  evidence for (skip dimensions with no evidence):
  - Layering (controller/service/repository, MVC, hexagonal, vertical-slice)
  - Naming conventions (class suffixes, method-naming, test-class naming)
  - Error handling (exceptions vs Result types, centralized handler, logging)
  - DI / wiring (Spring @Autowired vs constructor, framework, scope)
  - Configuration (application.yml / env vars / typed config, profiles, secrets)
  - Testing layout (location, integration markers, mock library, assertion style)
  - Logging / observability (framework, structured-log, MDC, metrics)
  - Persistence (ORM vs JDBC, migration tool, entity-naming)
  - HTTP / API style (REST/gRPC/GraphQL, URL conventions, DTO naming, OpenAPI)
  Cite >=2 representative file:line references per dimension. Do NOT cover
  every dimension — only those present in the surface you scanned.

Step 3 — Produce the artifact
  Write to {patterns_artifact} with this exact structure:
  - # Patterns to follow — task {task_id}
  - ## Surfaces scanned (bullet list of modules + target_repo paths)
  - ## Existing patterns (sub-sections per dimension found, each with:
        Pattern: <one line>
        Evidence:
        - file:line — what to look at
        - file:line — what to look at)
  - ## Pattern strength (mark each pattern as established / emerging / partial)
  - ## Out-of-scope observations (optional 2-3 neutral notes)

  Do NOT add recommendations or 'should consider' notes. If a dimension is
  missing, say so neutrally: `Pattern: not detected — Architect to decide and
  capture in ADR`.

Step 4 — Verify
  Confirm the file is >=30 lines, covers >=3 distinct dimensions, every claim
  cites >=2 file:line references.

Constraints:
- Read-only on application code — Read, Grep, Glob only (no Write/Edit/Bash)
- No new patterns; observe what exists
- Cite or skip — every claim has >=2 file:line citations
- Don't exhaust the codebase — 30-50 well-chosen file reads, not 1500
- Stay neutral — observe, do not judge

After writing the artifact, output the single line `PATTERNS_COMPLETE` and stop.
If verification fails, output `PATTERNS_INCOMPLETE: <reason>` and stop.

Cost target: under $1.50 on a medium target repo.
"

3. After the subagent finishes, verify {patterns_artifact} exists and is non-empty.
   If missing or empty, print `AGENT_ARTIFACT_MISSING` to stdout and exit non-zero.
   Otherwise print `AGENT_PATTERNS_DONE` to stdout and stop.

DO NOT do the pattern-detection yourself — your job is ONLY to dispatch the
Agent and verify its output. The pattern-detector subagent has Read/Grep/Glob
tools per its .claude/agents/pattern-detector.md definition.
""",

    "tasks": """You are running ONE pipeline stage (Task Breakdown) for task `{task_id}`.

Turn the approved architecture into a dependency-ordered, immediately
executable task list (`tasks.md`) that the Developer stage will implement
phase-by-phase. This is the Spec-Kit /tasks contract.

# References (vendored verbatim in this repo — read for canonical patterns)
- `{pipeline_root}/.claude/templates/spec-kit/tasks.md` — the upstream /tasks command
  (github/spec-kit). Defines the strict checklist format and phase structure
  below. The rules are inlined; you do not have to read the file to execute.

WHAT TO DO (steps 1-4 in order):

1. The architect subagent reads (skip silently if absent):
   - {task_dir}/01-ba.md (BRD: user stories + EARS acceptance criteria + FR/NFR)
   - {task_dir}/02-architecture.md (modules, per-module diffs, ADRs, test strategy)
   - {task_dir}/01b-patterns.md (existing patterns the tasks MUST respect)

2. Call the architect subagent (Winston) via the Agent tool:
   - subagent_type = "architect"
   - prompt = "
You are generating the implementation task breakdown (Spec-Kit /tasks) from an
approved BRD + architecture. Produce a dependency-ordered, immediately
executable task list — each task specific enough that a developer can complete
it without re-deciding anything.

Inputs:
- BRD: {task_dir}/01-ba.md
- Architecture: {task_dir}/02-architecture.md
- Patterns (if present): {task_dir}/01b-patterns.md

Write the task list to {tasks_artifact} with this EXACT structure:

## Phases
- Phase 1 — Setup: project init, dependencies, config.
- Phase 2 — Foundational: blocking prerequisites for ALL user stories.
- Phase 3+ — one phase per user story, in priority order from the BRD.
- Final Phase — Polish & cross-cutting: perf, docs, hardening.

## Tasks — every task MUST follow this checklist format EXACTLY:
  - [ ] T<NNN> [P]? [US<n>]? <description with an exact file path>
  * Checkbox `- [ ]` always; sequential id T001, T002, ... in execution order.
  * `[P]` ONLY if parallelizable (different files, no dependency on an
    incomplete task).
  * `[US<n>]` ONLY for user-story-phase tasks (maps to a BRD user story);
    Setup / Foundational / Polish tasks carry NO story label.
  * Every task names an exact file path.
  * TDD: when a story needs tests, the test task precedes its implementation
    task.

## Dependencies — story completion order + which phases block which.
## Parallel execution — example [P] groups that may run together.
## MVP — the minimum subset (typically User Story 1) that delivers value.

Stay strictly within the BRD scope + the architecture's chosen patterns; do
not invent scope. Write English.

Output MUST end with these EXACT lines (no other content after them):
TASKS_COMPLETE: <one-line summary>
TASK_COUNT: <total number of '- [ ]' tasks written>
"

3. After the subagent finishes, verify {tasks_artifact} exists, is non-empty,
   and contains at least one `- [ ] T` checklist line. If missing/empty, print
   `AGENT_ARTIFACT_MISSING` to stdout and exit non-zero.

4. Print the subagent's `TASKS_COMPLETE:` line, then `AGENT_TASKS_DONE`, to
   stdout and stop.

DO NOT implement anything — your job is ONLY to dispatch the Agent and verify
its output. The Developer stage executes this task list.
""",

    "analyze": """You are running ONE pipeline stage (Cross-Artifact Analyze) for task `{task_id}`.

Run a NON-DESTRUCTIVE cross-artifact consistency + quality analysis across the
BRD, architecture, and task list BEFORE the Developer implements. This is the
Spec-Kit /analyze contract — READ-ONLY except for writing the report.

# References (vendored verbatim in this repo — read for canonical patterns)
- `{pipeline_root}/.claude/templates/spec-kit/analyze.md` — the upstream /analyze command
  (github/spec-kit). Defines the detection passes, severity heuristic, and the
  report structure below. The rules are inlined; you need not read the file.

WHAT TO DO (steps 1-4 in order):

1. Call the architect subagent (Winston, implementation-readiness role) via the
   Agent tool:
   - subagent_type = "architect"
   - prompt = "
You are performing a STRICTLY READ-ONLY cross-artifact consistency analysis
(Spec-Kit /analyze). Do NOT modify any project files; your ONLY write is the
report at {analyze_artifact}.

Read:
- BRD (spec):     {task_dir}/01-ba.md
- Architecture:   {task_dir}/02-architecture.md
- Task list:      {task_dir}/02b-tasks.md
- Patterns (if present):     {task_dir}/01b-patterns.md
- Constitution (if present): {target_repo}/memory/constitution.md or {task_dir}/constitution.md

Run these detection passes over the three core artifacts:
- Duplication: near-duplicate requirements.
- Ambiguity: vague adjectives (fast/scalable/secure/robust) lacking measurable
  criteria; unresolved placeholders (TODO/TKTK/???).
- Underspecification: requirements missing object/measurable outcome; stories
  missing acceptance criteria; tasks referencing undefined files/components.
- Constitution alignment: any requirement/plan/task conflicting with a MUST
  principle. Constitution conflicts are ALWAYS CRITICAL.
- Coverage gaps: requirements (FR-/SC-) with zero associated task; tasks with
  no mapped requirement/story.
- Inconsistency: terminology drift; entities in the architecture absent from
  the spec; task-ordering contradictions; conflicting requirements.

Severity: CRITICAL (constitution MUST violation, or a requirement with zero
coverage that blocks baseline functionality); HIGH (duplicate/conflicting
requirement, ambiguous security/perf attribute, untestable AC); MEDIUM
(terminology drift, missing NFR task coverage, underspecified edge case); LOW
(wording / minor redundancy).

Write the report to {analyze_artifact} with EXACTLY this structure:

## Specification Analysis Report
| ID | Category | Severity | Location(s) | Summary | Recommendation |
(one row per finding; stable IDs prefixed by category initial; cap 50 rows.)

## Coverage Summary
| Requirement Key | Has Task? | Task IDs | Notes |

## Constitution Alignment Issues  (or 'None')
## Unmapped Tasks  (or 'None')

## Metrics
- Total Requirements / Total Tasks / Coverage % / Ambiguity Count /
  Duplication Count / Critical Issues Count

## Next Actions
- If CRITICAL issues exist: resolve them before implementation.
- Else: list improvement suggestions; the pipeline may proceed.

Do NOT modify spec/arch/tasks; do NOT hallucinate missing sections (report them
accurately); report zero issues gracefully with coverage stats. Write English.

Output MUST end with these EXACT lines (no other content after them):
ANALYZE_COMPLETE: <one-line summary>
CRITICAL_COUNT: <number of CRITICAL-severity findings>
"

2. After the subagent finishes, verify {analyze_artifact} exists and is
   non-empty. If missing/empty, print `AGENT_ARTIFACT_MISSING` to stdout and
   exit non-zero.

3. Read the trailing `CRITICAL_COUNT:` line from {analyze_artifact}.

4. Print the subagent's `ANALYZE_COMPLETE:` line and its `CRITICAL_COUNT:` line,
   then `AGENT_ANALYZE_DONE`, to stdout and stop.

DO NOT fix the findings yourself — this stage only reports. The pipeline gate
decides whether CRITICAL findings block the Developer.
""",

    "edge-cases": """You are running ONE pipeline stage (Edge Case Hunter) for task `{task_id}`.

Run the Edge Case Hunter methodology over the spec + architecture BEFORE the
Developer implements, so the implementation explicitly handles boundary
conditions. This is a discrete, method-driven pass (NOT adversarial review).

# References (vendored verbatim in this repo — read for canonical patterns)
- `{pipeline_root}/.claude/templates/bmad-v6/edge-case-hunter/SKILL.md` — the BMAD Edge Case
  Hunter: a pure path tracer that mechanically walks every branch + boundary
  and reports ONLY unhandled conditions (JSON array). Methodology inlined below.

WHAT TO DO (steps 1-3 in order):

1. Call the code-reviewer subagent via the Agent tool:
   - subagent_type = "code-reviewer"
   - prompt = "
You are the Edge Case Hunter — a PURE PATH TRACER. Never judge whether the
design is good or bad; ONLY enumerate boundary conditions the spec/architecture
does not yet explicitly handle. Method = exhaustive path enumeration, not
intuition.

Read:
- BRD (spec):   {task_dir}/01-ba.md
- Architecture: {task_dir}/02-architecture.md
- Task list (if present): {task_dir}/02b-tasks.md

Walk every branching path and domain boundary implied by the requirements +
design: missing else/default, null/empty/oversized inputs, off-by-one and
empty-collection loops, arithmetic overflow, implicit type coercion,
ordering / race / concurrency, timeout / retry / partial-failure,
auth / permission edges, and resource exhaustion. Derive the relevant edge
classes from the content itself; do not rely on a fixed checklist. Report ONLY
the conditions that are NOT explicitly handled — discard handled ones silently.

Output a JSON array; each object has EXACTLY these four fields:
[
  {{
    \"location\": \"<spec/arch section or FR-id the gap relates to>\",
    \"trigger_condition\": \"<one line, max 15 words>\",
    \"guard_snippet\": \"<minimal sketch of the guard/handling that closes it>\",
    \"potential_consequence\": \"<what goes wrong if unhandled, max 15 words>\"
  }}
]
An empty array [] is valid when nothing is unhandled — and if the BRD and
architecture are both absent or empty, return [] (nothing to trace). No prose
around the JSON.

After the JSON array, on a NEW line, output exactly:
EDGECASES_COMPLETE: <number of objects in the array>
"

2. Take the subagent's full response text (the JSON array + the
   EDGECASES_COMPLETE line) and write it verbatim to {edgecases_artifact}.
   Verify {edgecases_artifact} exists and is non-empty; if not, print
   `AGENT_ARTIFACT_MISSING` to stdout and exit non-zero.

3. Print the subagent's `EDGECASES_COMPLETE:` line, then `AGENT_EDGECASES_DONE`,
   to stdout and stop.

DO NOT fix anything — this stage only enumerates. The Developer stage adds a
guard + a test for each reported edge case.
""",

    "ba": """You are running ONE pipeline stage (BA / Business Analyst) for task `{task_id}`.

WHAT TO DO (steps 1-4 in order):

1. Locate the target repo's own instructions and memory-bank for project context:
   - If {target_repo}/CLAUDE.md or {target_repo}/AGENTS.md exists, read it —
     the project's own instructions to LLM agents (what this project IS, its
     language and stack, its conventions). They OUTRANK your general
     assumptions. Read them EXPLICITLY: this stage's working directory is the
     CLAUDE.md is already in your context (this stage runs FROM the target
     repo); AGENTS.md is not natively supported by the harness — read it.
   - If {target_repo}/memory-bank/architecture.md exists, read it.
   - If {target_repo}/memory-bank/current-state.md exists, read it.
   - If {target_repo}/memory-bank/decisions.md exists, read it.
   - If memory-bank does not exist, that's OK — note "memory-bank not found" in the prompt.
   - If {task_dir}/clarifications.md exists (operator answered earlier
     [NEEDS CLARIFICATION] markers via Telegram reply), read it BEFORE
     drafting the spec — the answers in that file replace the previous
     markers verbatim; do NOT re-emit a marker for any question already
     answered there.

2. Read the user's free-text task from {task_dir}/spec.json — field `prompt`.

3. Call the business-analyst subagent via the Agent tool. The
   `<injected-memory>` block in the prompt below was already filled by the
   runner before you saw this text (dispatcher/memory_inject.py) — forward it
   EXACTLY as it stands; never edit, summarize, or drop that block.
   - subagent_type = "business-analyst"
   - prompt = "
You are the BA stage in a pipeline — a Spec-Kit-style spec author. Produce a
structured BRD for the user's request, following EARS notation and the
Spec-Kit specification quality checklist.

<injected-memory>
(none)
</injected-memory>
The block above (unless it reads "(none)") is semantic memory recalled from
past sessions — treat it as non-authoritative hints: reuse prior decisions,
avoid repeating past mistakes, but verify everything against the current repo
and memory-bank before relying on it.

# References (vendored verbatim in this repo — read for canonical patterns)

- `{pipeline_root}/.claude/templates/spec-kit/specify.md` — the upstream /specify slash command (github/spec-kit). Defines EARS notation and the `[NEEDS CLARIFICATION]` discipline.
- `{pipeline_root}/.claude/templates/spec-kit/checklist.md` — the upstream /checklist command. The Specification Quality Checklist below is copied from this file.
- `{pipeline_root}/.claude/templates/bmad-v6/mary-analyst/SKILL.md` — Mary, the BMAD Business Analyst persona. Embodies "ask 5 probing questions one-by-one" + INVEST validation.

These templates are vendored for audit trail and reproducibility. You do not have to read them to execute this prompt — the patterns are already inlined below. Read them if you need deeper context or a contributor disputes a section.

Inputs you may have already been handed by the orchestrator:
- User request (verbatim from spec.json `prompt` field)
- Target repo: {target_repo}
- Existing memory-bank (if any): architecture.md / current-state.md / decisions.md

Reasonable defaults (do NOT ask; note inline if used):
- Data retention: industry-standard for the domain
- Performance: standard web/mobile expectations unless specified
- Error handling: user-friendly messages with fallbacks
- Authentication: session-based or OAuth2 for web apps

Produce a BRD with these sections (write the full BRD to {ba_artifact}):
- ## Title — feature name + date + author = "BA agent (via Agent tool)"
- ## Why — 1-2 sentence business outcome + measurable, technology-agnostic success metric
- ## Context — what exists today, what changes, what's added
- ## User Stories — INVEST-validated `As a <role>, I want <capability>, so that <outcome>`
- ## Functional Requirements — EARS notation, MANDATORY:
    * Ubiquitous:    `FR-NNN: THE System SHALL <response>`
    * Event-driven:  `FR-NNN: WHEN <trigger> THE System SHALL <response>`
    * State-driven:  `FR-NNN: WHILE <state> THE System SHALL <response>`
    * Unwanted:      `FR-NNN: IF <condition> THEN THE System SHALL <response>`
    * Optional:      `FR-NNN: WHERE <feature> THE System SHALL <response>`
  Each FR tagged `[Must]` / `[Should]` / `[Could]` / `[Won't]` (MoSCoW). Never
  use vague verbs (support / handle / consider).
- ## Non-Functional Requirements — EARS notation with measurable thresholds
- ## Acceptance Criteria — 1-3 per FR, concrete + testable, NO implementation detail
- ## Out of scope — explicit
- ## Dependencies and Assumptions
- ## Open Questions — CAPPED AT 3 `[NEEDS CLARIFICATION: ...]` markers; use defaults elsewhere

Then write `<task_dir>/checklists/requirements.md` with the Spec-Kit
Specification Quality Checklist (Content Quality / Requirement Completeness /
Feature Readiness sections — 3+8+3=14 checkbox items) PLUS a 4th section,
Theater Check (adapted from BMAD's PRD-validation rubric's "substance over
theater" dimension — steal-list §2.5, anti-over-formalization, NOT a Glossary/
UJ apparatus): mechanical self-checks you run yourself, in the same pass, no
elicitation, no halt —
- [ ] No NFR theater — every NFR has a product-specific, measurable threshold;
      none reads as boilerplate ("scalable" / "secure" / "reliable" alone,
      with no number attached)
- [ ] No persona/role theater — every named role in a User Story drives at
      least one FR; cut any role that exists only for narrative color
- [ ] No vision theater — the Why section's success metric is falsifiable
      (an observer could say it failed), not aspirational prose
Tick every box yourself, across all four sections. If any box stays empty,
revise the spec until it can be ticked — never leave a section partially
ticked and move on.

After self-validation passes, output the single line `BA_COMPLETE` followed by
a 1-2 sentence summary in your reply. The full BRD lives in {ba_artifact};
the checklist lives in {task_dir}/checklists/requirements.md.

"

4. After the subagent finishes, verify {ba_artifact} exists and is non-empty.
   If missing or empty, print `AGENT_ARTIFACT_MISSING` to stdout and exit non-zero.
   Otherwise print `AGENT_BA_DONE` to stdout and stop.

DO NOT write the BRD yourself — your job is ONLY to dispatch the Agent and
verify its output. The business-analyst subagent has Read/Write/Edit/Glob/
Grep/WebFetch/WebSearch tools per its .claude/agents/business-analyst.md
definition.
""",

    "security": """You are running ONE pipeline stage (Security audit) for task `{task_id}`.

WHAT TO DO (steps 1-4 in order):

1. Prepare the diff at {diff_path}:
   - cd {target_repo}
   - If PR is open: `gh pr diff {pr_number} > {diff_path}`
   - If PR is merged (state.json.stage == awaiting-approval): `gh pr view {pr_number}
     --json mergeCommit -q .mergeCommit.oid`, then
     `git show --pretty='' <sha>~..<sha> > {diff_path}`.
   - If neither works, fall back to `git diff main...HEAD > {diff_path}`.
   - Verify {diff_path} is non-empty.

2. Read {task_dir}/01-ba.md (the BRD) — keep its content for the next step.

3. Call the security-auditor subagent via the Agent tool:
   - subagent_type = "security-auditor"
   - prompt = "
You are the Security audit stage in a pipeline. Review the PR diff and
flag security-relevant issues.

Inputs:
- BRD: {task_dir}/01-ba.md
- PR diff: {diff_path}
- Target repo: {target_repo} (you may grep/glob for context)

Workflow:
1. Read the diff in full
2. Check categories:
   - Authentication / authorization changes
   - Input validation / injection surface
   - Secrets handling (env vars, credentials, tokens in logs)
   - Dependency changes (CVEs in new versions)
   - Logging / observability — anything that leaks PII / secrets
   - Authorization boundary crossings (RBAC, tenant isolation)
3. Categorize findings:
   - Critical — blocks merge (exploitable, secret leak, missing authz)
   - Warning — should fix but not block (outdated dep, missing rate limit)
   - Suggestion — defense-in-depth improvement
4. Filter scanner noise — only surface findings you can explain

Constraints:
- Read-only on application code (your tools allow Read/Grep/Glob — no
  Write/Edit; consistent with .claude/agents/security-auditor.md)
- Never report a finding without citing file + line + specific weakness

Write the full audit to {security_artifact}. Output MUST end with these
EXACT lines (no other content after them):
SECURITY_COMPLETE: <summary>
CRITICAL: <count>
WARNING: <count>
SUGGESTION: <count>
"

4. After the subagent finishes, verify {security_artifact} exists and is non-empty.
   If missing or empty, print `AGENT_ARTIFACT_MISSING` to stdout and exit non-zero.
   Otherwise print `AGENT_SECURITY_DONE` to stdout and stop.

DO NOT do the audit yourself — your job is ONLY to dispatch the Agent and
persist its output. The security-auditor subagent has Read/Grep/Glob per
its .claude/agents/security-auditor.md definition.
""",

    "tester": """You are running ONE pipeline stage (Tester) for task `{task_id}`.

PoC SAFETY: you may instruct the subagent to write test files into the target
repo, BUT you (the orchestrator) MUST NOT `git add`, `git commit`, or `git push`
anything. Production commit/push will be wired in Phase C. For now, the
artifact is informational.

WHAT TO DO (steps 1-5 in order):

1. Prepare the diff at {diff_path}:
   - cd {target_repo}
   - If PR is open: `gh pr diff {pr_number} > {diff_path}`
   - If PR is merged: `gh pr view {pr_number} --json mergeCommit -q .mergeCommit.oid`,
     then `git show --pretty='' <sha>~..<sha> > {diff_path}`.
   - If neither works: `git diff main...HEAD > {diff_path}`.
   - Verify {diff_path} is non-empty.

2. Read these for context:
   - {task_dir}/01-ba.md — the BRD with acceptance criteria
   - {task_dir}/03-dev.md (if exists) — Developer's summary of what was implemented
   - {target_repo}/memory-bank/decisions.md (if exists)
   - {target_repo}/CLAUDE.md, {target_repo}/AGENTS.md (if exist) — the
     project's own instructions to LLM agents. If they name the test command
     or framework, that is authoritative; do not infer a different one. NOT
     already in context for CLAUDE.md (this stage runs FROM the target repo);
     AGENTS.md is not natively supported by the harness — read it.

3. Identify the target repo's existing test infrastructure:
   - cd {target_repo}
   - Prefer what CLAUDE.md / AGENTS.md states. Otherwise look for pytest /
     unittest / vitest / jest / go test / cargo test / mvn / gradle — what
     test framework is in use?
   - Read any existing test files relevant to the area touched by the diff.
   - Note the test directory layout (tests/, __tests__, etc.).

4. Call the test-automator subagent via the Agent tool:
   - subagent_type = "test-automator"
   - prompt = "
You are the Tester stage in a pipeline. Add test coverage for the change in
the PR diff, focusing on the acceptance criteria in the BRD.

Inputs:
- BRD: {task_dir}/01-ba.md
- PR diff: {diff_path}
- Developer summary (if any): {task_dir}/03-dev.md
- Target repo: {target_repo}/

Workflow:
1. Read the diff and identify the modules / files that changed
2. Find the test framework in use (pytest, jest, etc.) by reading existing tests
3. Write additional tests covering:
   - Acceptance criteria from the BRD that have no test coverage yet
   - Edge cases / boundary conditions implied by the diff
   - Error paths the developer's diff added or modified
4. Save test files under the target repo's standard test directory (preserve
   existing layout — do NOT introduce a new tests/ folder if one already exists
   elsewhere). Use the Write tool for new files; Edit tool for additions to
   existing files.
5. Do NOT git add, commit, or push. The orchestrator will handle commit-or-not
   in a later phase. Your job is to PRODUCE the test files only.

Constraints:
- Run tests locally only if it's safe and fast (no slow E2E suites)
- If running tests is too expensive or sets up DB state, skip the run and just
  produce the files
- Match the project's existing testing conventions (assertion style, fixtures,
  mocking framework)

Write a structured summary to {tester_artifact} with these sections:
- ## Files written — list with absolute paths and 1-line description each
- ## Acceptance criteria covered — each FR-NNN mapped to the test that asserts it
- ## Edge cases added — bullet list
- ## Tests skipped — anything you could not run, with reason
- ## Follow-ups — tests you would write but ran out of time/context for

Output MUST end with these EXACT lines (no other content after them):
TEST_COMPLETE: <summary>
TESTS_ADDED: <count>
ACS_COVERED: <count>
"

5. After the subagent finishes, verify {tester_artifact} exists and is non-empty.
   If missing or empty, print `AGENT_ARTIFACT_MISSING` to stdout and exit non-zero.
   Otherwise print `AGENT_TESTER_DONE` to stdout and stop.

DO NOT git commit / push any of the test files the subagent wrote. The PoC
harness leaves them as unstaged changes in the target repo for inspection.
""",

    "developer": """You are running ONE pipeline stage (Developer) for task `{task_id}`.

# References (vendored verbatim in this repo — read for canonical patterns)
- `{pipeline_root}/.claude/templates/spec-kit/implement.md` — the upstream /implement command
  (github/spec-kit). The canonical 2026 implementation contract: execute the
  work phase-by-phase (Setup -> Tests -> Core -> Integration -> Polish), write
  tests BEFORE code, run file-coupled tasks sequentially and independent ones
  in parallel, and validate each phase before the next. The Workflow below is
  adapted from it.
- Testing methodology: example-based unit tests, PLUS property-based tests for
  any non-trivial pure logic. Use whatever the target's ecosystem provides —
  Hypothesis (Python), fast-check (JS/TS), jqwik (JVM), proptest (Rust),
  rapid (Go) — and skip the property-based layer if the target has no such
  library rather than adding a dependency for it.
You do not have to read these to execute this prompt — the patterns are
inlined below.
NOTE on refreshing from implement.md (committee Q5, 2026-06-02): this stage runs
HEADLESS via `claude -p`, so NEVER inline any implement.md step that issues an
interactive "STOP and ask (yes/no)" or otherwise waits for stdin (e.g. the
upstream `checklists/` gate at implement.md:54-84) — it would hang with no input.
Convergence is enforced by the Reviewer loop + the branch-base check, not by an
interactive prompt.

BRANCH & PR RULES — enforce these in the subagent's prompt and verify them in your post-subagent checks:
{branch_rule}
{pr_title_rule}
* Every `gh pr create` call MUST pass `--base {base_branch}` explicitly — NEVER
  omit the flag and rely on gh's repo-default fallback. A subagent that dropped
  this flag opened a real PR against the repo default instead of the
  registry-resolved base (issue #10, 2026-08-12); this is now checked and
  self-repaired post-stage, but the subagent must still pass it correctly.
* Commits land ONLY on that new branch — NEVER push to main/master.
* Conventional commit body. NO `Co-Authored-By` line. NO "Generated with Claude" or
  similar AI-attribution footer.

WHAT TO DO (steps 1-5 in order):

1. Read these inputs for grounding (skip silently if absent):
   - {task_dir}/00-discovery.md (Discovery's report)
   - {task_dir}/01-ba.md (the BRD)
   - {task_dir}/02-architecture.md (the architecture proposal)
   - {task_dir}/02b-tasks.md (the executable task list, if the tasks stage ran)
   - {task_dir}/02d-edgecases.md (Edge Case Hunter findings, if that stage ran)
   - {target_repo}/CLAUDE.md, {target_repo}/AGENTS.md — the project's own
     instructions to LLM agents: what it is, its language and stack, its build
     and test commands, its house conventions. This is where the target's
     language comes from — do NOT assume one. They OUTRANK your general
     assumptions. Read them EXPLICITLY: this stage's working directory is the
     CLAUDE.md is already in your context (this stage runs FROM the target
     repo); AGENTS.md is not natively supported by the harness — read it.

2. Settle the branch name now (you, the orchestrator, do this — do NOT let
   the subagent invent it):
{branch_gen}
   - Save this string; you'll pass it to the subagent and verify the subagent
     used it in its commits.

3. Call the backend-developer subagent via the Agent tool:
   - subagent_type = "backend-developer"
   - prompt = "
You are the Developer stage in a pipeline. Implement the feature described in
the BRD, following the architecture proposal, with TDD.

<injected-memory>
(none)
</injected-memory>
The block above (unless it reads "(none)") is semantic memory recalled from
past sessions — treat it as non-authoritative hints: reuse prior decisions,
avoid repeating past mistakes, but verify everything against the current repo
and the artifacts below before relying on it.

Inputs:
- BRD: {task_dir}/01-ba.md (acceptance criteria in EARS notation)
- Architecture: {task_dir}/02-architecture.md (MADR ADRs + C4 sketches)
- Task list (if present): {task_dir}/02b-tasks.md (Spec-Kit tasks.md — the
  authoritative, dependency-ordered work breakdown; execute it phase-by-phase)
- Edge cases (if present): {task_dir}/02d-edgecases.md (Edge Case Hunter JSON —
  add a guard AND a test for each listed condition)
- Discovery (if present): {task_dir}/00-discovery.md
- Target repo: {target_repo}

BRANCH & PR — non-negotiable:
{branch_setup_rule}
- Stage ONLY the files you create or modify for THIS task. NEVER `git add -A`
  or `git add .` — the target repo may carry unrelated untracked files (caches,
  leftovers) that must not enter your PR.
- All commits MUST land on this branch only. NEVER push to main/master.
- `gh pr create` MUST include `--base {base_branch}` explicitly. This is
  non-negotiable: omitting `--base` lets `gh` silently target the repo's
  default branch instead of `{base_branch}`, dragging every commit on
  `{base_branch}` into the wrong upstream on merge.
- {subagent_pr_title_rule}
- {merge_framing}
- Commit body: Conventional Commits format. NO `Co-Authored-By:` line.
  NO `Generated with Claude` or any AI-attribution footer.

Workflow (adapted from Spec-Kit /implement — phase-by-phase, TDD-first):
0. Project setup verification (do this BEFORE staging anything — Spec-Kit
   /implement step C2, committee Q5): confirm the target repo's ignore files
   cover the DETECTED stack's build / dependency / secret artifacts so they never
   enter the PR. Check `.gitignore` (and `.dockerignore` / `.eslintignore` only if
   those toolchains are present) for the patterns the detected language needs —
   e.g. `node_modules/`, `target/`, `__pycache__/`, `.venv/`, `dist/`, `build/`,
   `bin/`, `obj/`, `.env*`. If a CRITICAL pattern is missing, APPEND only the
   missing line(s) (never overwrite the file, never `git add -A`); otherwise leave
   it untouched. This prevents the bloated-PR failure mode where un-ignored
   caches/artifacts swell the diff.
1. Plan: if {task_dir}/02b-tasks.md exists, it is the AUTHORITATIVE task list —
   execute its phases in order and mark each `- [ ]` as `- [x]` when done. If it
   is absent, derive your own ordered task list from the BRD + architecture,
   grouped into phases (Setup -> Tests -> Core -> Integration -> Polish). Note
   which tasks are independent ([P]) vs. file-coupled (run sequentially).
2. Tests before code, one acceptance criterion at a time:
   - Write a failing test for the criterion (example-based; ADD a
     property-based test for any non-trivial pure logic, using the target
     ecosystem's library — see the testing methodology note above).
   - Run it to verify failure.
   - Write the minimal code to pass.
   - Run to verify pass.
   - 2-3 sentence self-critique; refactor if it identifies a concrete fix.
3. Complete each phase before starting the next; run the full project test
   suite at phase boundaries and fix regressions before proceeding.
4. Commit per Conventional Commits (per logical change, not one giant commit).
5. Open PR via `gh pr create --title '<title>' --body '<body>' --base {base_branch}`.
   `--base {base_branch}` is REQUIRED on this exact call — see the BRANCH & PR
   rule above; never issue `gh pr create` without it.
6. Capture the PR URL from gh output.

Constraints:
- TDD non-negotiable: no untested production code
- Stay within the BRD scope
- Never disable or skip a test to "make it pass"
- Use English in code, comments, commit messages
- After PR is open, write a 1-page summary to {dev_artifact} covering:
  * Files added/changed
  * Tests added (count + names)
  * Acceptance criteria coverage (FR-NNN ⇒ test name)
  * Decisions made within the architect's leeway

Output MUST end with these EXACT lines (no other content after them):
DEV_COMPLETE: <one-line summary>
BRANCH: <branch name you actually used>
PR_URL: <URL from gh pr create>
TESTS: <passed_count>/<total_count>
"

   The branch for this task is `{branch_name}`. Wherever the prompt above still
   says `<BRANCH_NAME_FROM_ORCHESTRATOR>`, substitute that name before passing
   the prompt to the Agent tool — never let the subagent invent one.

4. After the subagent finishes, verify {dev_artifact} exists. Read the trailing
   marker block and extract:
   - DEV_COMPLETE
   - BRANCH ({branch_verify_rule})
   - PR_URL (verify it starts with `https://github.com/`)
   - TESTS counts

   If BRANCH fails the rule above, that is a SAFETY violation.
   Print `AGENT_SAFETY_VIOLATION: branch=<actual>` to stdout and exit 5.

   If PR_URL is missing or not a github.com URL, print `AGENT_PR_MISSING`
   to stdout and exit 6.

   If the verification block can't be parsed at all, print
   `AGENT_ARTIFACT_UNPARSEABLE` and exit 7.

5. On success, print `AGENT_DEVELOPER_DONE` to stdout and stop.

DO NOT push to origin/main yourself. DO NOT merge anything. The PR stays
open for the human to inspect and close.
""",

    # ── Hotfix variant ────────────────────────────────────────────────────
    # Used by run_pipeline when iteration > 1 and the prior Reviewer verdict
    # was REQUEST_CHANGES. The subagent stays on the existing branch and
    # addresses findings from {task_dir}/06-review-agent.md — does NOT
    # create a new branch or open a new PR.
    "developer-hotfix": """You are running ONE pipeline stage (Developer, HOTFIX iteration {iteration}/{iteration_cap}) for task `{task_id}`.

This is a HOTFIX iteration. The Reviewer filed CRITICAL and/or WARNING
findings on the PR the initial Developer iteration opened. Address all of
them on the EXISTING branch (no new branch, no new PR).

BRANCH SAFETY — non-negotiable:
* DO NOT create a new branch. Stay on `{previous_branch}`.
* DO NOT open a new PR. Push commits to the existing PR `{previous_pr_url}`.
* Leave the existing PR title unchanged (set by the initial iteration).
* Conventional commits. NO `Co-Authored-By` line. NO AI-attribution footer.

WHAT TO DO (steps 1-5 in order):

1. Read the prior Reviewer's report at {task_dir}/06-review-agent.md.
   Identify every finding tagged Critical or Warning. The Suggestion-tier
   findings are optional — address them only if cheap. CRITICAL must be
   100% addressed; deferring even one means the hotfix fails.

2. Read these inputs for context (skip silently if absent):
   - {task_dir}/01-ba.md (acceptance criteria — re-confirm coverage didn't regress)
   - {task_dir}/02-architecture.md (architectural constraints still apply)
   - {task_dir}/03-dev-agent.md (your prior iteration summary — what tests already exist)
   - {target_repo}/CLAUDE.md, {target_repo}/AGENTS.md (the project's own
     instructions to LLM agents — language, stack, build/test commands, house
     conventions; CLAUDE.md is already in your context, this stage runs FROM
     the target repo — AGENTS.md is not natively supported by the harness, so
     read it)

3. Work on the existing branch:
{branch_setup_rule}

4. Call the backend-developer subagent via Agent tool:
   - subagent_type = "backend-developer"
   - prompt = "
You are the Developer (HOTFIX iteration {iteration}/{iteration_cap}). Address every
Critical and Warning finding from the prior Reviewer iteration. Stay on the
existing branch and push to the existing PR.

Inputs:
- Reviewer's findings: {task_dir}/06-review-agent.md
- BRD: {task_dir}/01-ba.md
- Architecture: {task_dir}/02-architecture.md
- Prior iteration summary: {task_dir}/03-dev-agent.md
- Target repo: {target_repo}
- Existing branch (DO NOT create new one): {previous_branch}
- Existing PR (DO NOT open new one): {previous_pr_url}

POC SAFETY — non-negotiable:
- You are already on `{previous_branch}`. Confirm with `git rev-parse --abbrev-ref HEAD`.
- All commits land on this branch. Push moves the existing PR forward.
- NEVER `git checkout -b ...`. NEVER `gh pr create ...`.
- NO `Co-Authored-By:` line. NO `Generated with Claude` footer.

Workflow (per finding, in order Critical → Warning → optional Suggestion):
1. Read the finding from {task_dir}/06-review-agent.md
2. Write a failing test that proves the bug exists (or expands an existing test if the
   finding is about coverage gaps rather than a defect)
3. Implement the minimal fix
4. Run the targeted test to confirm pass
5. Run the full project test suite (or the closest equivalent the project supports);
   fix any regressions before moving to the next finding
5b. Brownfield safety (Kiro Bugfix Spec): this is a brownfield change, NOT
    greenfield. For each fix, ALSO assert UNCHANGED behavior on adjacent
    untouched paths — add or keep an explicit regression test proving the
    surrounding behavior still passes. NEVER narrow or weaken a test to make a
    fix pass.
6. Commit per Conventional Commits, one commit per finding:
   `fix(scope): address Critical/Warning <ID-or-section> from reviewer iteration {iteration}-1`
7. `git push origin {previous_branch}` after each fix (so the PR thread updates live)

If a finding cannot be addressed (e.g. requires architecture change beyond your
scope), DO NOT silently skip. Document it in the hotfix summary's
'## Findings deferred' section with a 2-sentence rationale.

After all addressable findings are committed and pushed, write a hotfix
summary OVERWRITING {dev_artifact} with these sections:
- ## Iteration — `{iteration}` of `{iteration_cap}`
- ## Findings addressed — table: Severity | Finding | Commit SHA | Tests added
- ## Findings deferred — table or `None` (must be empty for Critical)
- ## Tests added (this iteration) — count + names
- ## Acceptance criteria — confirm initial BRD FR-NNN coverage didn't regress
- ## Notes — anything surprising about the codebase that surfaced this iteration

Output MUST end with these EXACT lines (no other content after them):
DEV_COMPLETE: iteration {iteration} — <one-line summary>
BRANCH: {previous_branch}
PR_URL: {previous_pr_url}
TESTS: <passed>/<total>
"

5. After the subagent finishes, verify:
   - {dev_artifact} exists, non-empty
   - BRANCH line in output == `{previous_branch}` (no drift)
   - PR_URL line == `{previous_pr_url}` (same PR)
   - If BRANCH or PR_URL drifted, print `AGENT_SAFETY_VIOLATION: branch/pr drift` and exit 5

6. Print `AGENT_DEVELOPER_DONE` to stdout and stop. Do NOT merge the PR.
""",
}

# ── Reviewer verdict regex (only used by reviewer stage) ──
VERDICT_RE = re.compile(
    r"REVIEW_COMPLETE:\s*(approve|request_changes)\s*\n"
    r"CRITICAL:\s*(\d+)\s*\n"
    r"WARNING:\s*(\d+)\s*\n"
    r"SUGGESTION:\s*(\d+)",
    re.IGNORECASE | re.MULTILINE,
)

# ── Stage-specific completion markers in orchestrator stdout ──
STAGE_DONE_MARKERS = {
    "discovery": "AGENT_DISCOVERY_DONE",
    "ba": "AGENT_BA_DONE",
    "pattern-detector": "AGENT_PATTERNS_DONE",
    "tasks": "AGENT_TASKS_DONE",
    "analyze": "AGENT_ANALYZE_DONE",
    "edge-cases": "AGENT_EDGECASES_DONE",
    "architect": "AGENT_ARCHITECT_DONE",
    "tester": "AGENT_TESTER_DONE",
    "security": "AGENT_SECURITY_DONE",
    "developer": "AGENT_DEVELOPER_DONE",
    "developer-hotfix": "AGENT_DEVELOPER_DONE",
    "reviewer": "AGENT_REVIEWER_DONE",
}
