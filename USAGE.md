# USAGE — running ai-delivery day to day

This is the operator's companion to [README.md](README.md). The README tells you *how to install* and lists every command; this document walks through the **actual workflows** you'll run after the system is up.

If you haven't installed yet, start with [ops/INSTALL.md](ops/INSTALL.md). Come back here once `systemctl status claude-tg-bot task-dispatcher watcher` is all green.

---

## Mental model

You drive ai-delivery through **three channels**, in roughly descending order of frequency:

```
Telegram chat (text or voice)   ← 80% of work: ad-hoc tasks, approvals
       │
       ▼
ai-delivery
       ▲
       │
Windmill cron flows             ← 15% of work: recurring tasks (nightly audits, weekly digests)
       │
       ▼
Manual spec.json drop           ← 5% of work: scripted/batch tasks, CI integration
```

All three produce the same artifact: a `tasks/inbox/<id>/spec.json` file. The dispatcher picks it up, runs the pipeline, and notifies you (Telegram) when human input is needed.

---

## Workflow 1 — submit a task from Telegram

The most common case. Open the bot chat:

**Text:**
```
/task @userbot fix the rate-limit retry — it's looping forever on 429
```

**Or free-form (no slash command):**
```
explain how our memory layer works
```
The bot decides whether this is a Q&A (meta-agent investigates without touching code) or a task (dispatched into the pipeline) based on the first verb.

**Or voice** — hold the mic, speak the request, release. Whisper STT transcribes; the bot routes the same way as text.

### What happens next

```
~1 min     Bot acknowledges, creates spec.json in tasks/inbox/
~30 sec    Dispatcher promotes to tasks/active/<id>/
~2–5 min   BA stage: clarifies requirements, may DM you on Telegram if unclear
~3–8 min   Architect: writes 02-architecture.md
~5–15 min  Developer: opens PR
~2–5 min   Tester + Security: in parallel; results in 04-test.md, 05-security.md
~2–4 min   Reviewer: final verdict (APPROVE or REQUEST_CHANGES, up to 3 hotfix loops)
```

Wall time end-to-end: typically 15–35 min for a small feature, 45–90 min for a refactor with multiple files. Cost: tracked in `state.json:costs`, shown to you on `/usage`.

### When BA needs clarification

You'll get a Telegram message:

> Task `tg-20260526-...` is in `awaiting-input/`. BA asks: «Should the retry use exponential backoff or jittered fixed delay?»

Reply in chat. The bot writes your answer back to the task and resumes the pipeline.

### When Reviewer approves the PR

```
[Да]  [Нет]
```

Inline keyboard appears with the PR URL. Tap `Да` → bot runs `gh pr merge --squash`, moves the task to `done/`. Tap `Нет` → task stays in `awaiting-approval/` for manual handling.

---

## Workflow 2 — multi-project routing

If you maintain several target repos, register them in `bot/projects.json`:

```json
{
  "_default": "userbot",
  "projects": {
    "userbot": "/home/you/projects/telegram-userbot-ai",
    "blog":    {"path": "/home/you/projects/personal-blog"},
    "api":     {"path": "/home/you/projects/internal-api", "base": "dev"}
  }
}
```

An entry is either a plain path string or `{"path": ..., "base": ...}`. `base`
pins the branch the pipeline cuts its work branch from **and** targets with the
PR — set it whenever development does not live on the repo's default branch.
Without it the base is `PIPELINE_BASE_BRANCH`, else the repo's own
`origin/HEAD`, else `main`.

Then:

```
/task @blog add a dark-mode toggle to the article reader
/task @api  refactor the rate-limiter middleware to use Redis tokens
```

Both run in parallel (up to `DISPATCHER_MAX_STAGES`, default 3 concurrent active tasks). Use `/projects` to list registered aliases.

Each task executes in its **own ephemeral `git worktree`** of the target repo
(under `/tmp/ai-delivery-wt/`, removed when the PR is pushed), so a run never
switches branches in the checkout you work in — including when the target repo
is ai-delivery itself.

---

## Workflow 3 — schedule a recurring task

For things you want done weekly/nightly without manual triggers (security audit, dependency bump check, log digest):

```
/schedule weekly-deps-audit "0 4 * * 1" Run dependabot audit on the userbot repo. Open a PR if anything is critical-or-higher.
```

The bot registers a Windmill flow. At 04:00 every Monday Windmill creates a `spec.json` in `tasks/inbox/`, the pipeline runs unattended, and you wake up to a PR.

Manage schedules in the Windmill UI (`http://localhost/` behind Caddy on a deployed host).

---

## Workflow 4 — manual spec drop (CI / scripting)

For programmatic submission — e.g., your monitoring system creates a task when an SLO breaches:

```bash
# Write to a temp location first, then atomically rename into inbox/
# (avoids the dispatcher reading a half-written file).
TASK_ID="manual-$(date +%s)"
TMPDIR="$(mktemp -d)"
mkdir -p "$TMPDIR/$TASK_ID"
cat > "$TMPDIR/$TASK_ID/spec.json" <<'EOF'
{
  "trigger": "manual",
  "user": "your-username",
  "task_id": "manual-1716743400",
  "prompt": "p99 latency on /search exceeded 500ms for 15 minutes. Investigate, propose fix.",
  "target_repo": "/home/you/projects/internal-api"
}
EOF
mv "$TMPDIR/$TASK_ID" tasks/inbox/
```

Omit `telegram_thread` (or set it to `null`) and there's no chat-routing — results land in `tasks/done/<id>/` for the operator to inspect. Supply `telegram_thread: {"chat_id": ..., "message_id": ...}` to opt into Telegram status updates. Cost/duration still recorded.

The canonical fixture is [`dispatcher/examples/spec.manual.example.json`](dispatcher/examples/spec.manual.example.json).

---

## Workflow 5 — handle a rate limit

When a backend hits 429:

```
[Switch anthropic→deepseek]  [Switch to glm]  [Schedule retry]  [Cancel]
```

| Choice | Effect |
|---|---|
| `Switch anthropic→deepseek` (or similar) | Immediately re-runs the current stage on the alternate backend. Task continues. |
| `Switch to glm` | Same, third backend. |
| `Schedule retry` | Windmill schedules a single-shot retry at `resetsAt` (parsed from the 429 response). Task stays in `awaiting-input/` until then. |
| `Cancel` | Moves task to `failed/` with reason. |

Default routing is in `dispatcher/routing.json`; you don't usually need to touch it. Auto-escalation: after 2 hotfix iterations on deepseek/glm, Dev+Test+Sec auto-flip to anthropic for the rest of that task.

---

## Workflow 6 — observe & debug

### Live activity

```bash
# Watch active tasks
watch -n 2 'ls -la tasks/active/'

# Follow a specific task's worklog
tail -f tasks/active/tg-20260526-*/worklog.md

# Tail dispatcher logs
journalctl -u task-dispatcher -f

# Tail bot logs
journalctl -u claude-tg-bot -f
```

### State inspection

```bash
# What is each task waiting on?
for d in tasks/awaiting-*/*; do
    echo "=== $d ==="
    jq '.status_reason' "$d/state.json"
done

# Per-task cost so far
jq '.costs' tasks/active/tg-*/state.json
```

### When something is stuck

1. **Check the watcher** — it's supposed to respawn dead stage_runners. `journalctl -u watcher -n 50`.
2. **Inspect the state file** — `cat tasks/active/<id>/state.json` shows the last attempted stage and error.
3. **Resume manually** — `bin/botctl-resume <task-id>` restarts the dispatcher loop for that one task (skips completed stages).
4. **Force a stage** — edit `state.json:stages.<name>.status` from `failed` to `pending` and the next dispatcher tick re-runs it.

### Cost & usage

```
/usage today    # all tasks finished today
/usage week     # last 7 days
/usage all      # everything in tasks/done/
```

Output groups by stage + backend, includes Claude API costs only (not LLM-as-a-service like DeepSeek which is billed separately). For cross-LLM cost, `ccusage` CLI is what `/usage` delegates to under the hood — run it directly for raw output.

---

## Workflow 7 — memory & recall

The system auto-captures every assistant reply, subagent output, and pre-compaction summary into a Qdrant vector store. Cross-task semantic memory.

```
/memo The voice transcription is unreliable for Russian numbers; user prefers digits over words.
/recall how did we handle the markdown→HTML preprocessor?
```

`/recall` returns top-5 matches with cosine scores. Cross-lingual (RU ↔ EN) verified at ~0.78-0.88. You generally don't need to write `/memo` manually — the four lifecycle hooks do it automatically.

---

## Workflow 8 — enable CodeGraph code intelligence (optional, one-time)

CodeGraph (`@colbymchenry/codegraph`) is a 100%-local code indexer: tree-sitter AST → SQLite + FTS5 + native file watcher (2s debounce). When enabled, the Discovery stage queries it before any blind file walks, which cuts Discovery token usage significantly on repos ≥10k LOC. Zero cloud egress, MIT licensed.

Use the idempotent setup script — it handles both the host MCP entry (`~/.claude.json`), the global CLI install (without which the bare-`codegraph` MCP entry can't launch), and the per-target init+index+gitignore in one go:

```
ops/setup-codegraph.sh                                  # host-only setup
ops/setup-codegraph.sh /path/to/target-repo             # host setup + init+index that target
```

Re-run on the same target later — it detects `.codegraph/` already exists and calls `codegraph sync` (incremental) instead of a full re-index. Verify after first run:

```
which codegraph                          # ~/.npm-global/bin/codegraph
codegraph --version                      # 0.9.x
ls /path/to/target/.codegraph/           # codegraph.db (SQLite+FTS5)
```

Restart the bot to pick up the new MCP server on the next dispatcher-spawned claude:

```
sudo systemctl restart claude-tg-bot task-dispatcher
```

The watcher syncs incrementally; you do not run `codegraph index` again unless the index gets confused (rare). If it does, `codegraph index --force` rebuilds.

When CodeGraph is not installed, the Discovery prompt falls back to grep/glob automatically — no flag change is needed.

**WSL2 caveat**: `inotify` is reliable on `/home/…` (ext4) but unreliable on `/mnt/c/…` (DrvFs). Keep your target repos under `/home/` for the watcher to fire.

---

## Tips

- **Run a task without Telegram**: copy `dispatcher/examples/spec.example.json` to `tasks/inbox/<your-id>/spec.json`, set `target_repo` to your repo, and the dispatcher ingests it like any other task — handy for a first dry run.
- **First task on a new target repo**: run a Q&A first ("describe this project's architecture") so the meta-agent builds context in `meta_agent_mem`. Subsequent tasks will be sharper.
- **Don't run more than 3 active tasks simultaneously** unless you bumped `DISPATCHER_MAX_STAGES` — beyond that, you'll start hitting rate limits across backends.
- **Voice messages work best when ≤30 seconds.** Longer recordings still transcribe but are more error-prone.
- **Backslash-n in prompts is interpreted literally** — write real newlines, not `\n`.
- **The Reviewer stage is the most expensive** — if you're iterating fast on a small change, consider `/task ... --skip-review` (when implemented) to short-circuit.

---

## Where to go deeper

- [README.md](README.md) — installation, command reference, conventions.
- [ARCHITECTURE.md](ARCHITECTURE.md) — system design, why-it-is-this-way.
- [ops/runbook.md](ops/runbook.md) — incident response, backup/restore, on-call procedures.
- [STATE/CURRENT.md](STATE/CURRENT.md) — what's actively being built / changed.
- [CONTRIBUTING.md](CONTRIBUTING.md) — if you want to send code back upstream.

---

## Reporting issues

If something doesn't match this document, the document is wrong. Open an issue with `docs: USAGE.md mismatch` and quote the section.
