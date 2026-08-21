# .claude/commands — slash-commands for parallel multi-agent workflows

Slash-commands picked up by `claude` CLI when run from this repo. All four
files in this directory come from
[wshobson/agents](https://github.com/wshobson/agents) — they're the
multi-agent orchestration commands that go with the `team-*` and `*-review`
agent set under `.claude/agents/`.

## Inventory

| Command         | What it does | Underlying agents |
|-----------------|-------------|-------------------|
| `/team-spawn`   | Spawn an agent team via preset (`review`, `debug`, `feature`, `fullstack`, `research`, `security`, `migration`) or custom composition; fans out parallel workstreams with file ownership boundaries | `team-lead` + N × `team-implementer`/`team-reviewer`/`team-debugger` |
| `/team-review`  | Multi-dimensional code review — spawns N reviewers in parallel, one per dimension (security / performance / architecture / testing / accessibility) and consolidates findings | `team-lead` + N × `team-reviewer` |
| `/team-debug`   | Hypothesis-driven debugging — generates N competing hypotheses for a failure mode and dispatches one investigator per hypothesis | `team-lead` + N × `team-debugger` |
| `/full-review`  | Comprehensive PR review with structured finding format (Critical / Warning / Suggestion); single-agent pass through 7+ review dimensions | `code-reviewer` |

## When you'd reach for these

- `/team-review` for the *pipeline's* Reviewer stage when you want
  parallel-fan-out depth instead of a single read-only pass. Today
  `stage_runner.py` calls a single `claude -p` for the reviewer stage; a
  future block can swap to `/team-review` for a richer multi-dimension
  consolidation.
- `/team-spawn feature` is the wshobson pattern that already informs Block 3.2
  (parallel Tester + Security via `asyncio.gather`). Same shape — file-ownership
  boundaries decided upfront, no shared writes — just executed in stage_runner
  rather than via Claude Code's slash command.
- `/team-debug` is useful when investigating a flaky pipeline failure — e.g.
  "stage_runner exits 1 sometimes after rate-limit recovery": 3-5 hypotheses,
  one investigator per hypothesis.
- `/full-review` is the alternative to `/team-review` when you want depth in
  *one* agent rather than breadth across many.

## Not used by stage_runner yet

Same caveat as the agent catalog: `stage_runner.py` still uses inline
`SYSTEM_PROMPTS` + direct subprocess invocation. These slash commands light
up when:

- you run `claude` interactively from this repo,
- the meta-agent inside `bot.py` is asked to fan out work (it can invoke
  slash commands via its tool surface),
- the `/main` Telegram channel routes to that meta-agent — so admins can
  type e.g. `/main /team-review the last PR for security and perf` and
  the orchestration runs inside ai-delivery's repo.
